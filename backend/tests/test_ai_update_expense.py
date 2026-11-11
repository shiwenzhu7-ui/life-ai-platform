"""A12：`update_expense` 改账 —— AI 侧第一个「改已有账目」的动作。

账没有标题，认的是「金额 + 时间 + 支付方式」，所以「改哪一笔」这件事
完全靠模型自己找对 id。这份测试因此不满足于「能改成功」，重心在：

1. 出卡那一刻账目一个字都不许动，卡片上得带着**改之前的样子**（`before` 快照）
2. 卡片要自己算好 `changed`（哪几项变了），而不是把两栏丢给前端比
3. 模型编 id / 抄别人的 id / 抄已软删的 —— 404，不落卡片
4. 用户确认后：只改那一笔、只写真的变了的字段（没改的字段不能把 updated_at 刷新）
5. override 能改值但改不了「改哪一笔」，白名单外的字段一律 422
6. 什么都没变时算成功，但不写库

全部 FakeProvider + 进程内 TestClient，不联网不花钱。
"""

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from conftest import API, make_expense, new_session

from app import app
from core.time import now_utc
from modules.lifeweb.ai.action_schema import UpdateExpensePayload
from modules.lifeweb.ai.models import PendingAction
from modules.lifeweb.ai.pending import (
    CODE_ALREADY_DONE,
    expense_before,
    expense_changed_fields,
    prepare_payload,
)
from modules.lifeweb.ai.provider import FakeProvider, LLMReply
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.expenses.model import Expense

# conftest 的种子分类 id 固定：1 = expense/餐饮，2 = expense/交通
CAT_FOOD_ID = 1
CAT_TRAFFIC_ID = 2


# ---------- 夹具 ----------


def _call(**arguments) -> dict:
    return {
        "id": "call_update_expense",
        "type": "function",
        "name": "update_expense",
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


def _chat(client, message: str = "把那笔 27.5 的打车费改成 30"):
    return client.post(f"{API}/ai/chat", json={"message": message})


def _reload(expense_id: int) -> Expense | None:
    """用新会话查，绕开 REPEATABLE READ 的旧快照。"""
    with new_session() as s:
        return s.get(Expense, expense_id)


def _pending_count() -> int:
    with new_session() as s:
        return s.query(PendingAction).count()


def _seed_pending(db, expense_id: int, *, status: str = "pending", minutes: int = 30, **extra):
    """造一张「改账」的草稿。

    payload 的形状要与 `pending.create_draft` 一致：顶层是「改成什么」，
    `before` 是「改之前什么样」，`before_category_name` 是出卡时查到的分类名。
    """
    before = {
        "expense_id": expense_id,
        "amount": "¥27.50",
        "type": "expense",
        "category_id": CAT_TRAFFIC_ID,
        "account": "微信",
        "occurred_at": "2026-11-08T21:00:00+08:00",
        "note": "打车回家",
    }
    action = PendingAction(
        user_id=1,
        session_id=1,
        message_id=1,
        action_type="update_expense",
        # 金额写字符串：真实链路里 `_parse_one` 用的是 `model_dump(mode="json")`，
        # Decimal 那时已经变成 "30.00"。手写夹具必须跟它一致，否则 JSON 列写不进去
        # —— 这也是这份夹具故意不调 `create_draft` 的代价，形状要自己盯住。
        payload={
            "expense_id": expense_id,
            "amount": "30.00",
            "before": before,
            "before_category_name": "交通",
            **extra,
        },
        status=status,
        idempotency_key=str(uuid.uuid4()),
        expires_at=now_utc() + timedelta(minutes=minutes),
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def _confirm(client, action_id: int, body: dict | None = None):
    return client.post(f"{API}/ai/actions/{action_id}/confirm", json=body or {})


def _make_taxi(db, **kw):
    """笔默认的「打车 27.5 微信」账目，多数用例都从这一笔出发。"""
    params = {
        "type_": "expense",
        "amount": "27.50",
        "category_id": CAT_TRAFFIC_ID,
        "account": "微信",
        "occurred_at": datetime(2026, 11, 8, 13, 0),  # naive UTC = 北京 21:00
        "note": "打车回家",
    }
    params.update(kw)
    return make_expense(db, **params)


# ---------- 出卡：卡片上要有「改成什么」和「改前是什么」 ----------


def test_chat_produces_update_card_with_before_snapshot(client, provider, db):
    """卡片必须同时给出改前与改后 —— 只有「改成 30」的话，用户不知道改的是哪一笔。"""
    expense = _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=expense.id, amount=30)]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    card = res.json()["pending_action"]
    assert card is not None, "改账必须出确认卡，不能直接改"
    assert card["action_type"] == "update_expense"

    preview = card["preview"]
    assert preview["expense_id"] == expense.id
    assert preview["amount_text"] == "¥30.00"
    assert preview["type_text"] == "支出"
    # 时间没说要改，卡片上仍要显示它原来在什么时候
    assert preview["occurred_at_text"] == "2026-11-08 21:00（周日）"

    before = preview["before"]
    assert before["amount"] == "¥27.50"
    assert before["amount_text"] == "¥27.50"
    assert before["account"] == "微信"
    assert before["type_text"] == "支出"
    assert before["occurred_at"] == "2026-11-08T21:00:00+08:00"
    assert before["occurred_at_text"] == "2026-11-08 21:00（周日）"


def test_card_lists_only_the_fields_that_changed(client, provider, db):
    """changed 是后端算好的「哪几项变了」，前端照它渲染 diff。"""
    expense = _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=expense.id, amount=30)]))

    preview = _chat(client).json()["pending_action"]["preview"]

    assert preview["changed"] == ["amount"], "只改了金额，别的都不能算进去"


def test_chat_card_is_only_a_draft(client, provider, db):
    """出卡那一刻，账目必须原封不动。"""
    expense = _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=expense.id, amount=30)]))

    assert _chat(client).status_code == 200

    assert str(_reload(expense.id).amount) == "27.50", "用户还没点确认，怎么就已经改了"


def test_changing_category_name_relooks_id_in_expense_scope(client, provider, db):
    """模型说「分类改成餐饮」：id 要在 expense 域里查，不能查成 task 域的「餐饮」。"""
    expense = _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=expense.id, category_name="餐饮")]))

    preview = _chat(client).json()["pending_action"]["preview"]

    assert preview["category_id"] == CAT_FOOD_ID
    assert preview["category_name"] == "餐饮"
    assert preview["changed"] == ["category_id"]
    # 原来的分类名要留在 before 侧，卡片才能显示「交通 → 餐饮」
    assert preview["before"]["category_id"] == CAT_TRAFFIC_ID
    assert preview["before_category_name"] == "交通"


def test_unknown_category_falls_back_to_uncategorized(client, provider, db):
    """用户/模型说了清单外的分类名：落 0 未分类，照样出卡（与记账一致）。"""
    expense = _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=expense.id, category_name="元宇宙")]))

    preview = _chat(client).json()["pending_action"]["preview"]

    assert preview["category_id"] == 0
    assert preview["changed"] == ["category_id"]


def test_repeating_the_same_values_produces_no_card(client, provider, db):
    """模型把原值又报了一遍：不出卡。

    这一条来自真机会话 130：模型抄了一遍原金额 30，库里那笔账本来就是 30，
    卡片上 `changed` 是空的，用户点确认得到「已按这张卡改好」，可一个字没动。
    与其让用户对着空 diff 猜，不如这一轮失败、由正文说清楚要改什么。
    """
    expense = _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=expense.id, amount="27.5", account="微信")]))

    res = _chat(client)

    assert res.status_code == 422, res.text
    assert "没有需要修改的内容" in res.json()["message"]
    assert _pending_count() == 0
    assert str(_reload(expense.id).amount) == "27.50"


def test_unknown_expense_id_is_404_and_no_card(client, provider, db):
    """模型编了一个不存在的 id：这一轮失败，不落卡片。"""
    _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=99999, amount=30)]))
    provider.queue(_reply(calls=[_call(expense_id=99999, amount=30)]))  # 重试还是假 id

    res = _chat(client)

    assert res.status_code == 404, res.text
    assert "不存在" in res.json()["message"]
    assert _pending_count() == 0


def test_other_users_expense_is_404(client, provider, db):
    """别人的账：对模型来说等同于不存在，也不会被人借卡片改掉。"""
    other = _make_taxi(db, user_id=2, note="别人的账")
    provider.queue(_reply(calls=[_call(expense_id=other.id, amount=1)]))
    provider.queue(_reply(calls=[_call(expense_id=other.id, amount=1)]))

    assert _chat(client).status_code == 404
    assert str(_reload(other.id).amount) == "27.50"


def test_deleted_expense_is_404(client, provider, db):
    """已经删掉的再改：404，不建一张注定点不动的卡。"""
    expense = _make_taxi(db)
    client.delete(f"{API}/expenses/{expense.id}")
    provider.queue(_reply(calls=[_call(expense_id=expense.id, amount=30)]))
    provider.queue(_reply(calls=[_call(expense_id=expense.id, amount=30)]))

    assert _chat(client).status_code == 404


def test_missing_expense_id_is_rejected(client, provider, db):
    """只说「改成 30」却不给 id：参数校验失败，走重试，仍失败则 502。"""
    _make_taxi(db)
    provider.queue(_reply(calls=[_call(amount=30)]))
    provider.queue(_reply(calls=[_call(amount=30)]))

    res = _chat(client)

    assert res.status_code == 502, res.text
    assert _pending_count() == 0


def test_no_change_at_all_is_rejected_by_the_schema():
    """只给 expense_id 等于「我想改但不知道改什么」，在 schema 层就该挡掉。"""
    with pytest.raises(Exception):
        UpdateExpensePayload(expense_id=1)


def test_note_can_be_cleared_but_not_other_fields():
    """备注能显式清空（空串有意义）；金额/时间清空在对话里没法可靠表达，不给。"""
    cleared = UpdateExpensePayload(expense_id=1, note="")
    assert cleared.changes() == {"note": None}

    same = UpdateExpensePayload(expense_id=1, amount=None, note=None)
    assert same.changes() == {"note": None}, "amount=None 是「不改」，note=None 是「清空」"


# ---------- 确认：只改那一笔、只写变了的字段 ----------


def test_confirm_updates_exactly_one_row(client, db):
    expense = _make_taxi(db)
    keep = _make_taxi(db, note="另一笔")
    action = _seed_pending(db, expense.id)

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "confirmed"
    assert body["created"] == {"type": "expense", "id": expense.id}
    assert body["created_id"] == expense.id

    assert str(_reload(expense.id).amount) == "30.00"
    assert str(_reload(keep.id).amount) == "27.50", "别的账一条都不能动"


def test_confirm_only_writes_changed_columns(client, db):
    """没变的字段不能被写一遍 —— 对账的人把 updated_at 当「这笔记动过」的证据。"""
    expense = _make_taxi(db)
    before = _reload(expense.id)
    action = _seed_pending(db, expense.id)

    assert _confirm(client, action.id).status_code == 200

    after = _reload(expense.id)
    assert str(after.amount) == "30.00"
    assert after.account == before.account
    assert after.note == before.note
    assert after.type == before.type
    assert after.category_id == before.category_id


def test_confirm_without_body_works(client, db):
    """改账卡不改也能确认（用卡片上的值）。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    res = client.post(f"{API}/ai/actions/{action.id}/confirm")

    assert res.status_code == 200, res.text
    assert str(_reload(expense.id).amount) == "30.00"


def test_override_changes_the_value_not_the_target(client, db):
    """override 能改金额，但 payload 里的 expense_id 不会被它改掉。"""
    expense = _make_taxi(db)
    other = _make_taxi(db, note="另一笔")
    action = _seed_pending(db, expense.id)

    res = _confirm(client, action.id, {"payload_override": {"amount": "35.00"}})

    assert res.status_code == 200, res.text
    assert str(_reload(expense.id).amount) == "35.00"
    assert str(_reload(other.id).amount) == "27.50"


def test_override_is_written_back_into_the_card(client, db):
    """改完的卡片要显示用户最终确认的值，不能还挂着模型原来报的 30。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    body = _confirm(client, action.id, {"payload_override": {"amount": "35.00"}}).json()

    assert body["preview"]["amount_text"] == "¥35.00"
    assert body["preview"]["before"]["amount"] == "¥27.50"


def test_override_cannot_change_expense_id(client, db):
    """想借 override 把卡片指到另一笔账上：expense_id 不在可改字段里，被忽略。"""
    expense = _make_taxi(db)
    other = _make_taxi(db, note="另一笔")
    action = _seed_pending(db, expense.id)

    res = _confirm(client, action.id, {"payload_override": {"expense_id": other.id, "amount": "1"}})

    assert res.status_code == 200, res.text
    assert str(_reload(expense.id).amount) == "1.00"
    assert str(_reload(other.id).amount) == "27.50", "另一笔账不能被改"


@pytest.mark.parametrize(
    "override",
    [
        {"user_id": 2},
        {"status": "done"},
        {"id": 999},
        {"created_at": None},
    ],
)
def test_override_rejects_unknown_fields(client, db, override):
    """override 里塞白名单外的字段：422，账目不动，卡片仍 pending。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    res = _confirm(client, action.id, {"payload_override": override})

    assert res.status_code == 422, res.text
    assert str(_reload(expense.id).amount) == "27.50"
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"


@pytest.mark.parametrize(
    "override",
    [
        {"amount": "0"},
        {"amount": "-5"},
        {"type": "transfer"},
        {"occurred_at": "2026-11-08T21:00:00"},  # 没时区
        {"occurred_at": "1900-01-01T00:00:00+08:00"},  # 年份越界
    ],
)
def test_override_rejects_bad_values(client, db, override):
    """override 的值本身不合法：422，不写库。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    res = _confirm(client, action.id, {"payload_override": override})

    assert res.status_code == 422, res.text
    assert str(_reload(expense.id).amount) == "27.50"


def test_prepare_payload_strips_preview_fields(client, db):
    """最终 payload 里不能带 before / amount_text / changed 这些预览字段。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    data = prepare_payload(db, action, None)

    assert "before" not in data
    assert "before_category_name" not in data
    assert "amount_text" not in data
    assert "changed" not in data
    assert data["expense_id"] == expense.id
    assert Decimal(str(data["amount"])) == Decimal("30.00")


def test_prepare_payload_ignores_unchanged_category_name(client, db):
    """分类名与原名一样时不该重查 —— 重查会让 category_id 从 2 漂到别的同名分类。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id, category_id=CAT_TRAFFIC_ID, category_name="交通")

    data = prepare_payload(db, action, None)

    assert data.get("category_id") in (None, CAT_TRAFFIC_ID)


def test_expense_before_rejects_other_users_id(db):
    """expense_before 是出卡时的归属校验入口：别人的 id 直接 404。"""
    other = _make_taxi(db, user_id=2)

    with pytest.raises(Exception) as caught:
        expense_before(db, 1, other.id)

    assert "不存在" in str(caught.value)


def test_changed_fields_ignores_formatting_differences():
    """27.5 与 ¥27.50 是同一个金额，不能算成「变了」。"""
    before = {"amount": "¥27.50", "occurred_at": "2026-11-08T21:00:00+08:00", "note": "x"}
    after = {"amount": Decimal("27.5"), "occurred_at": "2026-11-08T13:00:00+00:00", "note": "x"}

    assert expense_changed_fields(before, after) == []


# ---------- 状态机：一次都不能多改 ----------


def test_second_confirm_is_409(client, db):
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    assert _confirm(client, action.id).status_code == 200
    second = _confirm(client, action.id)

    assert second.status_code == 409
    assert second.json()["code"] == CODE_ALREADY_DONE


def test_expired_update_card_is_410(client, db):
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id, minutes=-1)

    res = _confirm(client, action.id)

    assert res.status_code == 410
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "expired"
    assert str(_reload(expense.id).amount) == "27.50"


def test_cancelled_update_card_cannot_confirm(client, db):
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)
    assert client.post(f"{API}/ai/actions/{action.id}/cancel", json={}).status_code == 200

    assert _confirm(client, action.id).status_code == 409
    assert str(_reload(expense.id).amount) == "27.50"


def test_expense_deleted_from_page_before_confirm_is_404(client, db):
    """卡片躺着的时候，用户在账本里把这笔删了：确认时 404，不是静默成功。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)
    client.delete(f"{API}/expenses/{expense.id}")

    res = _confirm(client, action.id)

    assert res.status_code == 404, res.text
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"


def test_confirm_writes_nothing_on_failure(client, db, monkeypatch):
    """业务写失败时卡片必须原地不动 —— 不能出现「卡片已确认、账没改」。"""
    from modules.lifeweb.ai import execute

    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    def _boom(*args, **kwargs):
        raise RuntimeError("数据库炸了")

    monkeypatch.setitem(execute._WRITERS, "update_expense", _boom)

    with pytest.raises(RuntimeError):
        _confirm(client, action.id)

    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"
    assert str(_reload(expense.id).amount) == "27.50"


# ---------- 兜底正文 ----------


def test_empty_content_says_update_not_create(client, provider, db):
    """兜底正文不能说成「我整理了一条待办」—— 这张卡点下去会改掉一笔已记的账。"""
    expense = _make_taxi(db)
    provider.queue(_reply(content=None, calls=[_call(expense_id=expense.id, amount=30)]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    assert res.json()["reply"] == "我整理了一条修改，请你确认。"


def test_model_words_are_kept_when_given(client, provider, db):
    """模型自己说了话，就用它的话，不拿兜底句盖掉。"""
    expense = _make_taxi(db)
    provider.queue(
        _reply(
            content="好的，我把打车费改成 30 了，你确认一下。",
            calls=[_call(expense_id=expense.id, amount=30)],
        )
    )

    res = _chat(client)

    assert res.json()["reply"] == "好的，我把打车费改成 30 了，你确认一下。"