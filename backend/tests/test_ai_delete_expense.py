"""A12：`delete_expense` 删账 —— AI 侧第二个「不可逆」的动作。

与 `test_ai_delete.py`（删待办）同一套骨架，差别在于**账没有标题**，
所以「要删的是哪一笔」只能靠金额 + 时间 + 支付方式认。这份测试盯的是：

1. 卡片上必须带着要删的那一笔的样子（`gone` 快照），否则「删那笔打车费」
   和「删那笔午饭」在卡上长得一样
2. 模型编 id / 别人的 id / 已软删的 —— 一律 404，不落卡片
3. 用户确认后：只软删那一笔，`deleted_at` 有值、行还在，金额不动
4. 删除卡不接受 override（和删待办一样）：这张卡没有「改成什么」可言
5. 重复确认 / 过期 / 取消，一次都不能多删

全部 FakeProvider + 进程内 TestClient，不联网不花钱。
"""

import json
import uuid
from datetime import datetime, timedelta

import pytest

from conftest import API, make_expense, new_session

from app import app
from core.time import now_utc
from modules.lifeweb.ai.models import PendingAction
from modules.lifeweb.ai.pending import CODE_ALREADY_DONE, prepare_payload
from modules.lifeweb.ai.provider import FakeProvider, LLMReply
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.expenses.model import Expense

# conftest 的种子分类 id 固定：2 = expense/交通
CAT_TRAFFIC_ID = 2


# ---------- 夹具 ----------


def _call(**arguments) -> dict:
    return {
        "id": "call_delete_expense",
        "type": "function",
        "name": "delete_expense",
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


def _chat(client, message: str = "把那笔打车费删了，记重了"):
    return client.post(f"{API}/ai/chat", json={"message": message})


def _reload(expense_id: int) -> Expense | None:
    with new_session() as s:
        return s.get(Expense, expense_id)


def _is_deleted(expense_id: int) -> bool:
    """用新会话查，绕开 REPEATABLE READ 的旧快照。"""
    return _reload(expense_id).deleted_at is not None


def _pending_count() -> int:
    with new_session() as s:
        return s.query(PendingAction).count()


def _seed_pending(db, expense_id: int, *, status: str = "pending", minutes: int = 30, **extra):
    """造一张「删账」的草稿，形状与 `pending.create_draft` 一致。"""
    gone = {
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
        action_type="delete_expense",
        payload={"expense_id": expense_id, "reason": "记重了", "gone": gone, **extra},
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


# ---------- 出卡：卡片上要有「删的是哪一笔」 ----------


def test_chat_produces_delete_card_with_snapshot(client, provider, db):
    """光有 expense_id 用户没法核对 —— 账没有标题，只能靠金额/时间/支付方式认。"""
    expense = _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=expense.id, reason="记重了")]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    card = res.json()["pending_action"]
    assert card is not None, "删除必须出确认卡，不能直接删"
    assert card["action_type"] == "delete_expense"

    preview = card["preview"]
    assert preview["expense_id"] == expense.id
    assert preview["reason"] == "记重了"

    gone = preview["gone"]
    assert gone["expense_id"] == expense.id
    assert gone["amount"] == "¥27.50"
    assert gone["amount_text"] == "¥27.50"
    assert gone["type_text"] == "支出"
    assert gone["account"] == "微信"
    assert gone["occurred_at_text"] == "2026-11-08 21:00（周日）"
    assert gone["note"] == "打车回家"


def test_chat_card_is_only_a_draft(client, provider, db):
    """出卡那一刻，账目必须原封不动。"""
    expense = _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=expense.id)]))

    assert _chat(client).status_code == 200

    assert not _is_deleted(expense.id), "用户还没点确认，怎么就已经删了"


def test_reason_is_optional(client, provider, db):
    """用户没说为什么删，卡片照样出，reason 为 None。"""
    expense = _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=expense.id)]))

    res = _chat(client, "这笔记错了吧")

    assert res.status_code == 200, res.text
    assert res.json()["pending_action"]["preview"]["reason"] is None


def test_unknown_expense_id_is_404_and_no_card(client, provider, db):
    """模型编了一个不存在的 id：这一轮失败，不落卡片。"""
    _make_taxi(db)
    provider.queue(_reply(calls=[_call(expense_id=99999)]))
    provider.queue(_reply(calls=[_call(expense_id=99999)]))  # 重试还是假 id

    res = _chat(client)

    assert res.status_code == 404, res.text
    assert "不存在" in res.json()["message"]
    assert _pending_count() == 0


def test_other_users_expense_is_404(client, provider, db):
    """别人的账：对模型来说等同于不存在。"""
    other = _make_taxi(db, user_id=2, note="别人的账")
    provider.queue(_reply(calls=[_call(expense_id=other.id)]))
    provider.queue(_reply(calls=[_call(expense_id=other.id)]))

    assert _chat(client).status_code == 404
    assert not _is_deleted(other.id)


def test_soft_deleted_expense_is_404(client, provider, db):
    """已经软删的再删一次：404，不建一张注定点不动的卡。"""
    expense = _make_taxi(db)
    with new_session() as s:
        s.get(Expense, expense.id).deleted_at = now_utc()
        s.commit()
    provider.queue(_reply(calls=[_call(expense_id=expense.id)]))
    provider.queue(_reply(calls=[_call(expense_id=expense.id)]))

    assert _chat(client).status_code == 404


def test_missing_expense_id_is_rejected(client, provider, db):
    """只说了「删掉」却不给 id：参数校验失败，走重试，仍失败则 502。"""
    _make_taxi(db)
    provider.queue(_reply(calls=[_call(reason="用户让删的")]))
    provider.queue(_reply(calls=[_call(reason="用户让删的")]))

    res = _chat(client)

    assert res.status_code == 502, res.text
    assert _pending_count() == 0


# ---------- 确认：软删，只删那一条 ----------


def test_confirm_soft_deletes_exactly_one_row(client, db):
    expense = _make_taxi(db)
    keep = _make_taxi(db, note="另一笔")
    action = _seed_pending(db, expense.id)

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "confirmed"
    assert body["created"] == {"type": "expense", "id": expense.id}
    assert body["created_id"] == expense.id

    assert _is_deleted(expense.id), "确认之后这笔账应当被软删"
    assert not _is_deleted(keep.id), "别的账一条都不能动"
    # 软删不是真删：行还在、金额不动
    row = _reload(expense.id)
    assert row is not None
    assert str(row.amount) == "27.50"


def test_confirm_without_body_works(client, db):
    """删除卡没有可改的字段，前端可以不带请求体。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    res = client.post(f"{API}/ai/actions/{action.id}/confirm")

    assert res.status_code == 200, res.text
    assert _is_deleted(expense.id)


def test_deleted_expense_disappears_from_list(client, db):
    """软删之后，账本列表里就看不见了。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)
    assert _confirm(client, action.id).status_code == 200

    listed = client.get(f"{API}/expenses").json()["items"]
    assert all(item["id"] != expense.id for item in listed)


def test_prepare_payload_keeps_only_expense_id_and_reason(client, db):
    """最终 payload 只认 expense_id 和 reason，卡片上的 gone 不进写库参数。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    data = prepare_payload(db, action, None)

    assert data == {"expense_id": expense.id, "reason": "记重了"}


@pytest.mark.parametrize(
    "override",
    [
        {"expense_id": 999},  # 换一笔删
        {"amount": "1.00"},  # 删除卡没有「改成什么」
        {"note": "随便"},
        {"user_id": 2},
        {"deleted_at": None},
        {},  # 空字典也算「想改内容」？不算 —— 见下面一条断言
    ],
)
def test_override_is_rejected_on_delete_cards(client, db, override):
    """删除卡一律不接受 override：前端没有可改的输入，有值就说明有东西不对。"""
    expense = _make_taxi(db)
    other = _make_taxi(db, note="另一笔")
    action = _seed_pending(db, expense.id)

    res = _confirm(client, action.id, {"payload_override": override})

    if override == {}:
        # 空字典在 `prepare_payload` 里按「没有 override」处理（`if override`）
        assert res.status_code == 200, res.text
        assert _is_deleted(expense.id)
        assert not _is_deleted(other.id)
        return

    assert res.status_code == 422, res.text
    assert "不支持修改" in res.json()["message"]
    assert not _is_deleted(expense.id)
    assert not _is_deleted(other.id)
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"


def test_confirm_writes_nothing_on_failure(client, db, monkeypatch):
    """业务写失败时卡片必须原地不动 —— 不能出现「卡片已确认、账还在」。"""
    from modules.lifeweb.ai import execute

    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    def _boom(*args, **kwargs):
        raise RuntimeError("数据库炸了")

    monkeypatch.setitem(execute._WRITERS, "delete_expense", _boom)

    with pytest.raises(RuntimeError):
        _confirm(client, action.id)

    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"
    assert not _is_deleted(expense.id)


# ---------- 状态机：一次都不能多删 ----------


def test_second_confirm_is_409(client, db):
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    assert _confirm(client, action.id).status_code == 200
    second = _confirm(client, action.id)

    assert second.status_code == 409
    assert second.json()["code"] == CODE_ALREADY_DONE


def test_expired_delete_card_is_410(client, db):
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id, minutes=-1)

    res = _confirm(client, action.id)

    assert res.status_code == 410
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "expired"
    assert not _is_deleted(expense.id)


def test_cancelled_delete_card_cannot_confirm(client, db):
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)
    assert client.post(f"{API}/ai/actions/{action.id}/cancel", json={}).status_code == 200

    res = _confirm(client, action.id)

    assert res.status_code == 409
    assert not _is_deleted(expense.id)


def test_cancel_does_not_touch_the_expense(client, db):
    """取消只是把卡片置 cancelled，业务表一个字不写。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)

    res = client.post(f"{API}/ai/actions/{action.id}/cancel", json={})

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "cancelled"
    assert not _is_deleted(expense.id)


def test_expense_deleted_from_page_before_confirm_is_404(client, db):
    """卡片躺着的时候，用户在账本里把这笔删了：确认时 404，不是静默成功。"""
    expense = _make_taxi(db)
    action = _seed_pending(db, expense.id)
    with new_session() as s:
        s.get(Expense, expense.id).deleted_at = now_utc()
        s.commit()

    res = _confirm(client, action.id)

    assert res.status_code == 404, res.text
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"


# ---------- 兜底正文 ----------


def test_empty_content_says_delete_not_create(client, provider, db):
    """兜底正文不能说成「我整理了一笔账」—— 这张卡点下去是删。"""
    expense = _make_taxi(db)
    provider.queue(_reply(content=None, calls=[_call(expense_id=expense.id)]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    assert res.json()["reply"] == "我整理了一条删除，请你确认。"


def test_model_words_are_kept_when_given(client, provider, db):
    """模型自己说了话，就用它的话，不拿兜底句盖掉。"""
    expense = _make_taxi(db)
    provider.queue(
        _reply(content="好的，我整理了一条删除，你确认一下。", calls=[_call(expense_id=expense.id)])
    )

    res = _chat(client)

    assert res.json()["reply"] == "好的，我整理了一条删除，你确认一下。"