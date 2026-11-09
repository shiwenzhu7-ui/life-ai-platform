"""把「缺信息一次问全」这条产品要求钉成测试。

为什么要测提示词文本：这句话的效果完全来自提示词（服务端不做补问逻辑），
而模型是个黑盒 —— 能做的最实在的防守是：只要有人把这句删掉或改弱，
测试立刻红，而不是等用户发现「它又开始一个一个问了」。

这些断言故意写得**不脆**：只钉关键短语，不钉完整句子，改措辞不会误伤。
"""

import json

import pytest

from modules.lifeweb.ai.prompts import TOOL_RULES, build_system_prompt
from modules.lifeweb.ai.tools import ALL_TOOLS, CREATE_EXPENSE, CREATE_TASK
from core.time import now_beijing


def _tool(name: str) -> dict:
    for tool in ALL_TOOLS:
        if tool["function"]["name"] == name:
            return tool["function"]
    raise AssertionError(f"没有这个工具：{name}")


def test_prompt_asks_all_missing_fields_at_once():
    rules = "\n".join(TOOL_RULES)
    assert "一次把缺的都问完" in rules, "这条要求没了，模型会退回一个一个问"
    assert "金额、用途、支付方式" in rules
    assert "要做什么、什么时候、属于哪一类" in rules


def test_prompt_stops_asking_after_one_round():
    """问过一轮就出卡：补了一种就按默认值填，别再追问细节。"""
    rules = "\n".join(TOOL_RULES)
    assert "问过一轮就够了" in rules
    assert "现金" in rules and "其他" in rules


def test_system_prompt_contains_the_rule_in_real_build():
    """真正发出去的那段 system message 里得有它（别只加在常量上）。"""
    text = build_system_prompt(now_beijing(), {"expense": ["餐饮"], "task": ["工作"]})
    assert "一次把缺的都问完" in text


def test_create_expense_requires_amount():
    params = CREATE_EXPENSE["function"]["parameters"]
    assert params["required"] == ["amount"], "金额缺了没法制卡，必须问；其余由后端兜底"
    assert "一次把缺的全问完" in CREATE_EXPENSE["function"]["description"]


def test_expense_account_has_no_ask_demand():
    """账户字段不能再写「没提就问」——那会让模型在有金额时也不出卡。"""
    desc = CREATE_EXPENSE["function"]["parameters"]["properties"]["account"]["description"]
    assert "没提" in desc and "省略" in desc


def test_create_task_requires_title():
    params = CREATE_TASK["function"]["parameters"]
    assert params["required"] == ["title"]
    assert "一次把缺的全问完" in CREATE_TASK["function"]["description"]


def test_tool_declarations_are_still_json_clean():
    """提示词里的引号/换行不能把工具声明弄成非法 JSON（要原样发给 DeepSeek）。"""
    payload = json.dumps(ALL_TOOLS, ensure_ascii=False)
    assert json.loads(payload) == ALL_TOOLS


# ---------- 优先级规则（C 方案：可解释的三档，不凭感觉） ----------


def _priority_desc() -> str:
    return CREATE_TASK["function"]["parameters"]["properties"]["priority"]["description"]


def test_priority_rule_is_explainable():
    desc = _priority_desc()
    assert "3 天内" in desc and "high" in desc
    assert "更远" in desc and "medium" in desc
    assert "没有任何时间" in desc and "low" in desc


def test_priority_defers_to_explicit_user_words():
    desc = _priority_desc()
    assert "很急" in desc and "不急" in desc, "用户明说时必须以用户为准"


def test_priority_is_never_asked():
    """优先级不用问用户 —— 否则又回到「一个一个问」的老路。"""
    assert "不用问" in _priority_desc()


def test_priority_enum_still_matches_model():
    """三档枚举必须和 tasks 模型的 PRIORITIES 一致，否则写了也存不进去。"""
    from modules.lifeweb.tasks.model import TASK_PRIORITIES

    enum = CREATE_TASK["function"]["parameters"]["properties"]["priority"]["enum"]
    assert tuple(enum) == tuple(TASK_PRIORITIES)


def test_update_task_priority_has_no_ask_demand():
    """update 的 priority 是「用户没提就不要传」，不能被出卡规则污染。"""
    params = _tool("update_task")["parameters"]["properties"]
    assert "没提就不要传" in params["priority"]["description"]