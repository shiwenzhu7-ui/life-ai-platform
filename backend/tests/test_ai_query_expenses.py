"""A9：`query_expenses` 只读查账 —— 让助手答得出「这个月花了多少」。

`test_ai_query.py` 证明的是「待办查得准」；这边证明「**钱算得准**」。
全部用 FakeProvider，不联网不花钱。要证明的六件事：

1. 合计按**全部命中记录**算，不受明细条数上限影响（最容易错、错了最难看的一条）
2. 时间范围按北京时间的整天切，边界那一笔不多算也不少算
3. 分类按 expense 域查（「购物」是 3 不是 task 域的 18），不存在的分类直接空结果
4. 只算自己的、只算没删的
5. 查账不建卡、不写业务表；参数坏掉时降级为无筛选而不是让整条消息失败
6. 模型查完不说话时，兜底正文里要有合计，而不只是一串明细
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import API, make_expense, new_session

from app import app
from modules.lifeweb.ai.models import PendingAction
from modules.lifeweb.ai.provider import FakeProvider, LLMReply
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.expenses.model import Expense

CST = timezone(timedelta(hours=8))


def _call(name: str = "query_expenses", **arguments) -> dict:
    return {
        "id": f"call_{name}",
        "type": "function",
        "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }


def _reply(content=None, *, calls=None, tokens=(10, 5), **kw) -> LLMReply:
    return LLMReply(
        content=content, tool_calls=calls, token_in=tokens[0], token_out=tokens[1], **kw
    )


@pytest.fixture()
def provider():
    fake = FakeProvider()
    app.dependency_overrides[get_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_provider, None)


def _chat(client, message: str = "这个月花了多少", session_id: int | None = None):
    body = {"message": message}
    if session_id is not None:
        body["session_id"] = session_id
    return client.post(f"{API}/ai/chat", json=body)


def _fed(provider: FakeProvider, call_index: int = 1) -> str:
    """第 `call_index` 次模型调用收到的最后一条消息 —— 就是回灌的查询结果。"""
    return provider.calls[call_index]["messages"][-1]["content"]


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    """北京时间某天某点的 naive UTC —— 库里存的就是这个。"""
    return (
        datetime(year, month, day, hour, tzinfo=CST).astimezone(timezone.utc).replace(tzinfo=None)
    )


def _expense_count() -> int:
    with new_session() as s:
        return s.query(Expense).count()


# ---------- 合计与明细 ----------


def test_total_counts_every_match_not_just_the_page(client, provider, db):
    """12 笔、明细只要 2 笔：合计必须是 12 笔的和，行文里也要说清「共 12 笔」。

    这是这个工具最容易出错的地方 —— 把 limit 当成「一共就这些」，
    用户在页面上看到的是 12 笔，助手却说「一共 3 笔」。
    """
    for i in range(12):
        make_expense(db, amount="10.00", occurred_at=_at(2026, 11, 1, hour=i + 1))

    provider.queue(_reply(None, calls=[_call(limit=2)])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "共 12 条" in fed
    assert "¥120.00" in fed, "合计按全部 12 笔算，不是这 2 笔"
    assert fed.count("金额=") == 2, "明细按 limit 截断"


def test_no_limit_hint_when_everything_fits(client, provider, db):
    """全部命中都列出来了，就不能再说「明细只列了最近的一部分」。"""
    make_expense(db, amount="8.50", occurred_at=_at(2026, 11, 3))

    provider.queue(_reply(None, calls=[_call()])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "明细只列了最近的一部分" not in fed
    assert "¥8.50" in fed


def test_expense_and_income_are_totalled_apart(client, provider, db):
    """收入和支出分开合计：混在一起算出来的「结余」是错的。"""
    make_expense(db, amount="30.00", type_="expense", occurred_at=_at(2026, 11, 2))
    make_expense(db, amount="20.00", type_="expense", occurred_at=_at(2026, 11, 3))
    make_expense(db, amount="5000.00", type_="income", category_id=10, occurred_at=_at(2026, 11, 4))

    provider.queue(_reply(None, calls=[_call()])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "支出合计：¥50.00" in fed
    assert "收入合计：¥5000.00" in fed


def test_type_filter_only_keeps_that_side(client, provider, db):
    make_expense(db, amount="30.00", type_="expense", occurred_at=_at(2026, 11, 2))
    make_expense(db, amount="5000.00", type_="income", category_id=10, occurred_at=_at(2026, 11, 4))

    provider.queue(_reply(None, calls=[_call(type="income")])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "¥5000.00" in fed and "共 1 条" in fed


# ---------- 时间范围 ----------


def test_date_range_is_beijing_whole_days(client, provider, db):
    """「11 月 1 日到 11 月 3 日」= 北京时间的 1 号 00:00 到 4 号 00:00，两端整天都算。

    10 月 31 日 23:59 和 11 月 4 日 00:01 各放一笔在边界外，
    只要换算差一个时区，数字就全错。
    """
    make_expense(db, amount="1.00", occurred_at=_at(2026, 10, 31, 23))  # 范围外
    make_expense(db, amount="10.00", occurred_at=_at(2026, 11, 1, 0))  # 边界内（1 号 00:00）
    make_expense(db, amount="20.00", occurred_at=_at(2026, 11, 3, 23))  # 边界内（3 号 23:00）
    make_expense(db, amount="2.00", occurred_at=_at(2026, 11, 4, 1))  # 范围外

    provider.queue(
        _reply(None, calls=[_call(date_from="2026-11-01", date_to="2026-11-03")])
    ).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "共 2 条" in fed
    assert "¥30.00" in fed


def test_reversed_range_is_swapped_not_dropped(client, provider, db):
    """起止反了（模型把先后说反）要换过来，而不是退化成「查全部」。"""
    make_expense(db, amount="10.00", occurred_at=_at(2026, 11, 2))
    make_expense(db, amount="99.00", occurred_at=_at(2026, 12, 20))  # 不在区间里

    provider.queue(
        _reply(None, calls=[_call(date_from="2026-11-05", date_to="2026-11-01")])
    ).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "共 1 条" in fed and "¥10.00" in fed


def test_datetime_string_is_treated_as_a_date(client, provider, db):
    """模型偶尔传 `2026-11-01T00:00:00+08:00`，要按 11-01 这一整天处理。"""
    make_expense(db, amount="10.00", occurred_at=_at(2026, 11, 1, 9))
    make_expense(db, amount="20.00", occurred_at=_at(2026, 11, 2, 9))

    provider.queue(
        _reply(None, calls=[_call(date_from="2026-11-01T00:00:00+08:00")])
    ).queue(_reply("好的"))
    assert _chat(client).status_code == 200
    assert "共 2 条" in _fed(provider)


# ---------- 分类与账户 ----------


def test_category_resolves_in_expense_scope(client, provider, db):
    """「购物」在 expense 域是 3、在 task 域是 18 —— 查错域会静默返回 0 条。"""
    make_expense(db, amount="30.00", category_id=3, occurred_at=_at(2026, 11, 2))  # expense/购物
    make_expense(db, amount="40.00", category_id=1, occurred_at=_at(2026, 11, 2))  # expense/餐饮

    provider.queue(_reply(None, calls=[_call(category_name="购物")])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "共 1 条" in fed and "¥30.00" in fed


def test_unknown_category_returns_nothing_and_says_so(client, provider, db):
    """不存在的分类不能退化成「未分类」去查 —— 那会返回一堆无关的账。"""
    make_expense(db, amount="30.00", category_id=1, occurred_at=_at(2026, 11, 2))

    provider.queue(_reply(None, calls=[_call(category_name="养猫")])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "没有名为「养猫」的分类" in fed
    assert "¥30.00" not in fed


def test_account_filter(client, provider, db):
    make_expense(db, amount="12.00", account="微信", occurred_at=_at(2026, 11, 2))
    make_expense(db, amount="13.00", account="现金", occurred_at=_at(2026, 11, 2))

    provider.queue(_reply(None, calls=[_call(account="微信")])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "共 1 条" in fed and "¥12.00" in fed


# ---------- 隔离 ----------


def test_other_users_and_deleted_rows_are_invisible(client, provider, db):
    """别人的账、自己删掉的账，一条都不能进合计 —— 否则「一共多少」直接泄露。"""
    make_expense(db, amount="100.00", user_id=2, occurred_at=_at(2026, 11, 2))
    gone = make_expense(db, amount="200.00", occurred_at=_at(2026, 11, 2))
    client.delete(f"{API}/expenses/{gone.id}")
    make_expense(db, amount="9.90", occurred_at=_at(2026, 11, 2))

    provider.queue(_reply(None, calls=[_call()])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "共 1 条" in fed
    assert "¥9.90" in fed
    assert "100.00" not in fed and "200.00" not in fed


# ---------- 边界与兜底 ----------


def test_empty_result_says_so(client, provider, db):
    provider.queue(_reply(None, calls=[_call(date_from="2030-01-01")])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "共 0 条" in fed and "没有符合条件的记录" in fed


def test_query_does_not_write_anything(client, provider, db):
    make_expense(db, amount="10.00", occurred_at=_at(2026, 11, 2))

    provider.queue(_reply(None, calls=[_call()])).queue(_reply("这个月花了 10 块。"))
    resp = _chat(client)
    assert resp.status_code == 200
    assert resp.json()["pending_action"] is None

    with new_session() as s:
        assert s.query(PendingAction).count() == 0
        assert _expense_count() == 1, "查账不能多记一笔账"


def test_bad_arguments_degrade_to_unfiltered_query(client, provider, db):
    """limit 是垃圾值不能让整条消息失败：降级成无筛选，并在回灌里说清原因。"""
    make_expense(db, amount="10.00", occurred_at=_at(2026, 11, 2))

    bad = {
        "id": "call_bad",
        "type": "function",
        "name": "query_expenses",
        "arguments": json.dumps({"limit": "abc", "type": "买了就吃"}),
    }
    provider.queue(_reply(None, calls=[bad])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert "共 1 条" in fed, "降级后仍然查到了"
    # 文案里是「没通过校验」（_fold_messages 原文），不能断言成「没有通过」
    assert "没通过校验" in fed


def test_limit_is_clamped_to_ten(client, provider, db):
    """模型传 limit=100 也只列 10 条明细（请求体不能被撑爆）。"""
    for i in range(15):
        make_expense(db, amount="1.00", occurred_at=_at(2026, 11, 1, hour=i + 1))

    provider.queue(_reply(None, calls=[_call(limit=100)])).queue(_reply("好的"))
    assert _chat(client).status_code == 200

    fed = _fed(provider)
    assert fed.count("金额=") == 10
    assert "共 15 条" in fed


def test_silent_model_falls_back_to_totals(client, provider, db):
    """模型查完一个字没说：兜底正文里必须有合计，而不只是一串明细。"""
    make_expense(db, amount="30.00", occurred_at=_at(2026, 11, 2))

    provider.queue(_reply(None, calls=[_call()])).queue(_reply(None))
    resp = _chat(client)
    assert resp.status_code == 200

    reply = resp.json()["reply"]
    assert "¥30.00" in reply
    assert "支出合计" in reply


def test_query_expenses_is_declared_read_only():
    """工具声明与只读名单必须对上，否则 tool_flow 会把它当成写入工具。"""
    from modules.lifeweb.ai.tools import ALL_TOOLS, READ_ONLY_TOOLS, TOOL_NAMES

    assert "query_expenses" in TOOL_NAMES
    assert "query_expenses" in READ_ONLY_TOOLS
    tool = next(t for t in ALL_TOOLS if t["function"]["name"] == "query_expenses")
    params = tool["function"]["parameters"]
    assert params["required"] == [], "一个条件都不给也要能查"
    assert params["properties"]["limit"]["maximum"] == 10
    assert params["properties"]["type"]["enum"] == ["expense", "income"]


def test_parse_date_accepts_the_shapes_models_send():
    from modules.lifeweb.ai.tool_flow import _parse_date

    assert _parse_date("2026-11-01") == date(2026, 11, 1)
    assert _parse_date("2026-11-01T00:00:00+08:00") == date(2026, 11, 1)
    assert _parse_date(datetime(2026, 11, 1, 15)) == date(2026, 11, 1)
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("下周一") is None