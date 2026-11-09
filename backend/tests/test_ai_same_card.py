"""把「这张新卡是不是上面那张的重复」的判据钉成测试（A11 补丁 · 单卡规则细化）。

真机会话 116：用户先说「帮我记个待办：买水果」，模型出了卡；紧接着用户说
「帮我记个待办：周五下午三点去银行」，模型也规规矩矩调了工具 —— 却被
「同一会话只留一张卡」这条规则拦下，回了一句「上面那张卡还在等你确认」，
用户要的那件事凭空消失。

所以拦不拦要看内容：同一件事重复发 → 拦；换了一件事 → 放行（旧卡过期）。
判据故意从宽（只比类型 + 标题 / 金额+分类）：把「改了措辞的同一件事」
误判成新事，代价只是旧卡过期、屏幕上仍只有一张可点的卡；
反过来把两件不同的事判成重复，代价是用户的话消失 —— 后者严重得多。
"""

from datetime import datetime
from decimal import Decimal

from modules.lifeweb.ai import pending
from modules.lifeweb.ai.models import PendingAction


def _card(action_type: str, payload: dict) -> PendingAction:
    """只填判断用得上的字段：is_same_item 不碰数据库。"""
    return PendingAction(action_type=action_type, payload=payload, status="pending")


def test_same_title_is_the_same_item():
    card = _card("create_task", {"title": "买水果"})
    assert pending.is_same_item("create_task", {"title": "买水果"}, card) is True


def test_whitespace_and_case_do_not_matter():
    card = _card("create_task", {"title": "买 水果"})
    assert pending.is_same_item("create_task", {"title": "买水果"}, card) is True
    english = _card("create_task", {"title": "Buy Milk"})
    assert pending.is_same_item("create_task", {"title": "buy milk"}, english) is True


def test_a_different_title_is_a_different_item():
    card = _card("create_task", {"title": "买水果"})
    assert pending.is_same_item("create_task", {"title": "去银行"}, card) is False


def test_different_action_types_are_never_the_same_item():
    """「删除买水果」和「新建买水果」是两件事，哪怕标题一样。"""
    card = _card("create_task", {"title": "买水果"})
    assert pending.is_same_item("delete_task", {"title": "买水果"}, card) is False


def test_delete_cards_compare_the_gone_snapshot():
    """删除卡的 payload 里没有 title，标题在 gone 快照里。"""
    card = _card("delete_task", {"task_id": 7, "gone": {"title": "买水果"}})
    assert pending.is_same_item("delete_task", {"task_id": 7, "gone": {"title": "买水果"}}, card)
    other = pending.is_same_item("delete_task", {"task_id": 8, "gone": {"title": "去银行"}}, card)
    assert other is False


def test_expenses_compare_amount_and_category():
    card = _card("create_expense", {"amount": Decimal("27.50"), "category_name": "交通"})
    same = {"amount": Decimal("27.50"), "category_name": "交通"}
    assert pending.is_same_item("create_expense", same, card) is True

    other_amount = {"amount": Decimal("30.00"), "category_name": "交通"}
    assert pending.is_same_item("create_expense", other_amount, card) is False

    other_category = {"amount": Decimal("27.50"), "category_name": "餐饮"}
    assert pending.is_same_item("create_expense", other_category, card) is False


def test_unknown_shape_falls_back_to_allowing_the_new_card():
    """指纹取不到（老数据没 title）时当作新事 —— 宁可多一张卡，也不吞掉用户的话。"""
    card = _card("create_task", {})
    assert pending.is_same_item("create_task", {"title": "去买菜"}, card) is False


def test_parse_skips_duplicates_but_keeps_the_new_one():
    """一次回复里先重申旧的、再说新的：跳旧的，留新的。

    真机会话 118 的原始调用（模型一口气交了两个）：
        create_task 买水果  +  create_task 去银行
    「一次只接一张卡」的截断如果发生在这之前，用户要的「去银行」就被踢掉了。
    """
    from modules.lifeweb.ai.action_schema import parse_tool_calls
    from modules.lifeweb.ai.provider import LLMReply

    import json

    reply = LLMReply(
        content="",
        tool_calls=[
            {"id": "c1", "type": "function", "name": "create_task",
             "arguments": json.dumps({"title": "买水果"}, ensure_ascii=False)},
            {"id": "c2", "type": "function", "name": "create_task",
             "arguments": json.dumps({"title": "去银行"}, ensure_ascii=False)},
        ],
    )
    old = _card("create_task", {"title": "买水果"})

    kept = parse_tool_calls(
        reply, skip=lambda t, p: pending.is_same_item(t, p, old)
    )
    assert len(kept) == 1
    assert kept[0].payload["title"] == "去银行"


def test_parse_without_skip_still_caps_at_one_draft():
    """不传 skip 时行为不变：仍然只采纳第一份草稿。"""
    from modules.lifeweb.ai.action_schema import parse_tool_calls
    from modules.lifeweb.ai.provider import LLMReply

    import json

    reply = LLMReply(
        content="",
        tool_calls=[
            {"id": "c1", "type": "function", "name": "create_task",
             "arguments": json.dumps({"title": "买水果"}, ensure_ascii=False)},
            {"id": "c2", "type": "function", "name": "create_task",
             "arguments": json.dumps({"title": "去银行"}, ensure_ascii=False)},
        ],
    )
    kept = parse_tool_calls(reply)
    assert len(kept) == 1
    assert kept[0].payload["title"] == "买水果"


def test_expire_open_drafts_marks_only_pending_rows(client, db):
    """旧卡让位是「标 expired」，不是删除：用户后来问起要答得出来。"""
    from core.time import now_beijing, to_utc_naive
    from modules.lifeweb.ai.models import ChatSession

    session = ChatSession(user_id=1, title="让位测试", message_count=0)
    db.add(session)
    db.flush()
    db.add_all(
        [
            PendingAction(
                user_id=1,
                session_id=session.id,
                message_id=1,
                action_type="create_task",
                payload={"title": "买水果"},
                status="pending",
                idempotency_key="k1",
                expires_at=to_utc_naive(now_beijing().replace(year=datetime.now().year + 1)),
            ),
            PendingAction(
                user_id=1,
                session_id=session.id,
                message_id=1,
                action_type="create_task",
                payload={"title": "已确认的那张"},
                status="confirmed",
                idempotency_key="k2",
                expires_at=to_utc_naive(now_beijing().replace(year=datetime.now().year + 1)),
            ),
        ]
    )
    db.commit()

    changed = pending.expire_open_drafts(db, 1, session.id)
    db.commit()

    assert changed == 1
    rows = {row.idempotency_key: row.status for row in db.query(PendingAction).all()}
    assert rows["k1"] == "expired"
    assert rows["k2"] == "confirmed", "已处理的卡不该被让位改状态"