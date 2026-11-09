"""A5 第 1 步：工具声明的结构约束。

这里没有业务逻辑，测的是「下几步的代码依赖了哪些约定」：
  - 能被 json.dumps（第 4 步要塞进 httpx 的 json= 参数）
  - name 唯一、「type」固定、parameters 是 object 且有必填项
  - TOOL_NAMES 与 ALL_TOOLS 一致（第 3 步靠它挡幻觉工具名）

description 的文字写得合不合适没法断言，只能到第 4 步用真机验。
"""

import json
from copy import deepcopy

import pytest

from modules.lifeweb.ai.tools import (
    ALL_TOOLS,
    CREATE_TASK,
    QUERY_TASKS,
    READ_ONLY_TOOLS,
    TOOL_NAMES,
)


def test_all_tools_is_json_serializable():
    """塞进 httpx 请求体前必须先序列化成功，否则等运行时才炸。"""
    dumped = json.dumps(ALL_TOOLS, ensure_ascii=False)

    # 中文 description 不该被转义成 \uXXXX：转义后 token 数会涨
    assert "创建一条新的待办事项" in dumped
    assert json.loads(dumped) == ALL_TOOLS


def test_every_tool_is_well_formed():
    """每条工具声明都符合 OpenAI 兼容的 function calling 结构。"""
    for tool in ALL_TOOLS:
        assert tool["type"] == "function"

        function = tool["function"]
        assert function["name"]
        assert function["description"]

        parameters = function["parameters"]
        assert parameters["type"] == "object"
        # 写入类至少要有一个必填参数（没标题就建不出东西）；
        # 只读查询可以零必填 —— 「我还有啥没做」本来就没有关键词可填
        if function["name"] not in READ_ONLY_TOOLS:
            assert parameters["required"], f"{function['name']} 至少要有一个必填参数"
        # 必填项必须真的在 properties 里，否则模型填不出来
        for name in parameters["required"]:
            assert name in parameters["properties"], f"{name} 不在 properties 里"
        assert parameters["additionalProperties"] is False


def test_tool_names_are_unique():
    names = [tool["function"]["name"] for tool in ALL_TOOLS]
    assert len(names) == len(set(names))


def test_tool_names_matches_all_tools():
    assert TOOL_NAMES == frozenset(tool["function"]["name"] for tool in ALL_TOOLS)
    assert isinstance(TOOL_NAMES, frozenset)


def test_create_task_title_is_the_only_required_field():
    """title 必填、其余三项可选——多一个必填就多一次模型漏填导致的坏草稿。"""
    parameters = CREATE_TASK["function"]["parameters"]
    assert parameters["required"] == ["title"]
    assert set(parameters["properties"]) == {"title", "due_at", "priority", "category_name"}


def test_create_task_field_names_match_task_module():
    """参数名与 tasks 模块同名字段，全链路一个词。"""
    from modules.lifeweb.tasks.schema import TaskCreate

    tool_fields = set(CREATE_TASK["function"]["parameters"]["properties"])
    assert {"title", "due_at", "priority"} <= set(TaskCreate.model_fields)
    # category_name 是 AI 侧的名字，落库前要换成 tasks 的 category_id
    assert "category_name" in tool_fields
    assert "category_id" not in tool_fields


def test_create_task_priority_enum_matches_model():
    """enum 与数据库 ENUM 一字不差，否则模型填出来的值会被数据库拒绝。"""
    from modules.lifeweb.tasks.model import TASK_PRIORITIES

    enum = CREATE_TASK["function"]["parameters"]["properties"]["priority"]["enum"]
    assert tuple(enum) == TASK_PRIORITIES


def test_tools_are_not_mutated_by_callers():
    """ALL_TOOLS 会被服务层直接传给 provider，provider 不该也没法改它。

    这里只防「测试或代码不小心改了模块级常量」：
    深拷贝一份改掉，原对象必须不受影响。
    """
    copy = deepcopy(ALL_TOOLS)
    copy[0]["function"]["name"] = "tampered"
    assert ALL_TOOLS[0]["function"]["name"] == "create_task"


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_each_tool_name_is_a_plain_identifier(name):
    """工具名会出现在日志、确认记录、前端预览里，必须是纯标识符。"""
    assert name.isidentifier()
    assert name.islower()


# ---------- A6：只读工具 ----------


def test_query_tasks_requires_no_argument():
    """查询必须能零参数调用 —— 用户说「我还有啥没做」时没有关键词可填。"""
    parameters = QUERY_TASKS["function"]["parameters"]
    assert parameters["required"] == []
    assert set(parameters["properties"]) == {
        "keyword",
        "status",
        "category_name",
        "due_before",
        "limit",
    }


def test_query_tasks_limit_is_capped():
    """结果会回灌进下一轮请求体，上限必须卡死在 10。"""
    limit = QUERY_TASKS["function"]["parameters"]["properties"]["limit"]
    assert limit["maximum"] == 10
    assert limit["minimum"] == 1


def test_read_only_tools_is_a_strict_subset():
    """只读名单只能从真实工具里挑，且必须包含 query_tasks。"""
    assert READ_ONLY_TOOLS < TOOL_NAMES
    assert "query_tasks" in READ_ONLY_TOOLS
    assert "create_task" not in READ_ONLY_TOOLS


def test_query_tasks_description_forbids_lying_about_writes():
    """说明书里得明说「不要说已经帮你改好」，否则模型查完就邀功。"""
    description = QUERY_TASKS["function"]["description"]
    assert "只读" in description
    assert "create_task" in description