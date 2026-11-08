"""A5 第 2 步：工具调用解析（纯函数，不起数据库）。

重点不是「好的能过」，而是「各种坏数据都不崩、都返回空」。
模型返回的 arguments 是不可信输入，这里每一条用例都是它真实会吐的东西。
"""

from types import SimpleNamespace

import pytest

from modules.lifeweb.ai.action_schema import (
    ACTION_TYPE_BY_TOOL,
    MAX_DRAFTS_PER_REPLY,
    CreateTaskPayload,
    parse_tool_calls,
)


def make_reply(*calls: dict) -> SimpleNamespace:
    """只带 tool_calls 的假 LLMReply —— 解析只用这一个属性。"""
    return SimpleNamespace(content=None, tool_calls=list(calls))


def make_call(arguments, *, name="create_task", call_id="call_1") -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


GOOD_ARGS = (
    '{"title":"去超市","due_at":"2026-11-09T15:00:00+08:00",'
    '"priority":"high","category_name":"购物"}'
)


# ---------------------------------------------------------------- 正常路径


def test_parses_full_call():
    drafts = parse_tool_calls(make_reply(make_call(GOOD_ARGS)))

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.tool_call_id == "call_1"
    assert draft.tool_name == "create_task"
    assert draft.action_type == "create_task"
    assert draft.payload == {
        "title": "去超市",
        "due_at": "2026-11-09T15:00:00+08:00",
        "priority": "high",
        "category_name": "购物",
    }


def test_parses_title_only_and_fills_defaults():
    drafts = parse_tool_calls(make_reply(make_call('{"title":"交报告"}')))

    assert len(drafts) == 1
    assert drafts[0].payload == {
        "title": "交报告",
        "due_at": None,
        "priority": "medium",
        "category_name": None,
    }


def test_payload_is_json_safe():
    """payload 要直接存进 JSON 列：datetime 必须是字符串，不能是 datetime 对象。"""
    import json

    drafts = parse_tool_calls(make_reply(make_call(GOOD_ARGS)))
    json.dumps(drafts[0].payload)  # 不抛即通过


def test_title_is_stripped():
    drafts = parse_tool_calls(make_reply(make_call('{"title":"  去超市  "}')))
    assert drafts[0].payload["title"] == "去超市"


def test_draft_is_frozen():
    """草稿一旦成型不该再被改（后面几步都只读它）。"""
    draft = parse_tool_calls(make_reply(make_call(GOOD_ARGS)))[0]
    with pytest.raises(Exception):
        draft.tool_name = "tampered"


# ---------------------------------------------------------------- 坏数据


@pytest.mark.parametrize(
    ("arguments", "why"),
    [
        ('{"title":"去超市",}', "多一个逗号"),
        ("not json at all", "根本不是 JSON"),
        ('```json\n{"title":"x"}\n```', "裹了代码块"),
        ("[]", "合法 JSON 但不是对象"),
        ('"abc"', "是个字符串"),
        ("123", "是个数字"),
        (None, "None"),
        ({"title": "x"}, "已经是 dict（本轮约定只收字符串）"),
    ],
)
def test_bad_arguments_are_dropped_not_raised(arguments, why):
    """坏输入返回空列表，绝不抛异常 —— 抛了就是整条消息 500。"""
    assert parse_tool_calls(make_reply(make_call(arguments))) == [], why


@pytest.mark.parametrize(
    ("arguments", "why"),
    [
        ('{"title":""}', "空标题"),
        ('{"title":"   "}', "纯空格标题"),
        ('{"title":"' + "长" * 121 + '"}', "标题超过 120"),
        ('{"due_at":"2026-11-09T15:00:00"}', "缺 title"),
        ('{"title":"开会","due_at":"2026-11-09T15:00:00"}', "时间不带时区"),
        ('{"title":"开会","due_at":"0206-11-09T15:00:00+08:00"}', "年份 0206"),
        ('{"title":"开会","due_at":"2260-11-09T15:00:00+08:00"}', "年份 2260"),
        ('{"title":"开会","priority":"urgent"}', "枚举外的优先级"),
        ('{"title":"开会","category_name":"' + "名" * 33 + '"}', "分类名超长"),
        ('{"name":"开会"}', "字段名不对"),
        ('{"title":"开会","tags":["工作"]}', "模型自加的字段"),
        ('{"title":"开会","remind_before":30}', "模型自加的字段（2）"),
    ],
)
def test_invalid_payloads_are_dropped(arguments, why):
    assert parse_tool_calls(make_reply(make_call(arguments))) == [], why


def test_unknown_tool_name_is_dropped():
    """模型幻觉出不存在的工具名。"""
    assert parse_tool_calls(make_reply(make_call(GOOD_ARGS, name="delete_all_tasks"))) == []


def test_missing_name_is_dropped():
    assert parse_tool_calls(make_reply({"id": "x", "arguments": GOOD_ARGS})) == []


def test_non_dict_call_is_dropped():
    assert parse_tool_calls(make_reply("not a call")) == []


# ---------------------------------------------------------------- 多条


def test_empty_tool_calls_returns_empty_list():
    assert parse_tool_calls(make_reply()) == []
    assert parse_tool_calls(SimpleNamespace(content="普通回复", tool_calls=[])) == []
    assert parse_tool_calls(SimpleNamespace(content="没有这个属性")) == []


def test_bad_call_does_not_block_good_one():
    """第一条坏了，第二条照样能用。"""
    drafts = parse_tool_calls(
        make_reply(
            make_call("{bad json", call_id="call_bad"),
            make_call('{"title":"交报告"}', call_id="call_good"),
        )
    )
    assert len(drafts) == 1
    assert drafts[0].tool_call_id == "call_good"


def test_only_first_draft_is_kept():
    """A5 简化规则：一次回复只采纳第一份草稿。"""
    drafts = parse_tool_calls(
        make_reply(
            make_call('{"title":"买菜"}', call_id="call_1"),
            make_call('{"title":"交报告"}', call_id="call_2"),
        )
    )
    assert MAX_DRAFTS_PER_REPLY == 1
    assert [d.tool_call_id for d in drafts] == ["call_1"]


# ---------------------------------------------------------------- 契约


def test_action_type_mapping_covers_every_payload_model():
    """每个能解析的工具都必须有 action_type 映射，否则 KeyError。"""
    from modules.lifeweb.ai.action_schema import PAYLOAD_MODELS
    from modules.lifeweb.ai.tools import TOOL_NAMES

    assert set(ACTION_TYPE_BY_TOOL) == set(PAYLOAD_MODELS)
    assert set(PAYLOAD_MODELS) <= set(TOOL_NAMES), "解析器不该接受未声明的工具"


def test_payload_fields_match_tool_declaration():
    """payload 的字段集合必须与 tools.py 声明的 properties 完全一致。"""
    from modules.lifeweb.ai.tools import CREATE_TASK

    declared = set(CREATE_TASK["function"]["parameters"]["properties"])
    assert declared == set(CreateTaskPayload.model_fields)


def test_payload_has_no_user_or_status_field():
    """模型不能决定「属于谁」「状态是什么」，这些由服务端定。"""
    assert "user_id" not in CreateTaskPayload.model_fields
    assert "status" not in CreateTaskPayload.model_fields
    assert "category_id" not in CreateTaskPayload.model_fields