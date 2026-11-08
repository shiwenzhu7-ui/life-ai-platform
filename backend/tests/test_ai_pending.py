"""A5 第 3 步：草稿落库（ai_pending_actions）。

这一层开始连库，但仍然**不写任何业务数据**：整份文件跑完，`tasks` 表应该还是空的。
测的是四件事：

1. 分类名能换成 id，换不到就落 0 —— 不因为分类缺失整张卡作废
2. 存进 payload 的可读文本是对的（北京时间、星期、优先级中文），且不改入参
3. `create_draft` 只 flush 不 commit —— 由 service 决定什么时候提交
4. 状态机只有 pending 能走：重复确认不覆盖、过期后确认不了、越权查不到
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from core.errors import AppError
from core.time import now_utc
from modules.lifeweb.ai.action_schema import parse_tool_calls
from modules.lifeweb.ai.models import ChatMessage, ChatSession, PendingAction
from modules.lifeweb.ai.pending import (
    DEFAULT_TTL_MINUTES,
    build_preview,
    create_draft,
    format_due_text,
    get_pending,
    mark_cancelled,
    mark_confirmed,
    resolve_category_id,
)
from modules.lifeweb.tasks.model import Task
from tests.conftest import new_session

# conftest 的种子分类里 id 是固定的：15 = task/工作，18 = task/购物，3 = expense/购物
TASK_WORK_ID = 15
TASK_SHOPPING_ID = 18


# ---------- 夹具 ----------


def _make_session_row(db: Session, *, user_id: int = 1, title: str = "新对话") -> ChatSession:
    session = ChatSession(user_id=user_id, title=title)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_assistant_message(db: Session, session_id: int, *, user_id: int = 1) -> ChatMessage:
    message = ChatMessage(
        user_id=user_id, session_id=session_id, role="assistant", content="我帮你记一条"
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def _draft(arguments: str, *, tool_name: str = "create_task"):
    """借用第 2 步的解析器造一份真草稿，不手拼 ToolDraft。

    这样测试走的是「模型返回 → 校验 → 草稿」的真实路径；如果哪天
    action_schema 改了字段，这里会直接失败，而不是构造出一个没人会产出的草稿。
    """

    class _Reply:
        tool_calls = [{"id": "call_1", "name": tool_name, "arguments": arguments}]

    drafts = parse_tool_calls(_Reply())
    assert len(drafts) == 1, "夹具构造失败：arguments 没能通过第 2 步校验"
    return drafts[0]


def _seed_pending(db: Session, *, user_id: int = 1, status: str = "pending", **payload) -> PendingAction:
    """直接写一行 pending（绕过 create_draft），用于状态机测试。"""
    action = PendingAction(
        user_id=user_id,
        session_id=1,
        message_id=1,
        action_type="create_task",
        payload=payload or {"title": "去超市"},
        status=status,
        idempotency_key=str(uuid.uuid4()),
        expires_at=now_utc() + timedelta(minutes=30),
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


# ---------- 分类名 → id ----------


def test_resolve_known_category_name(db):
    assert resolve_category_id(db, "create_task", "工作") == TASK_WORK_ID


def test_resolve_category_scoped_to_task_domain(db):
    """「购物」在 task 和 expense 两个域都有；待办必须命中 task 那条。"""
    assert resolve_category_id(db, "create_task", "购物") == TASK_SHOPPING_ID


def test_resolve_unknown_category_falls_back_to_zero(db):
    """模型编出来的分类名不该让整张卡片作废。"""
    assert resolve_category_id(db, "create_task", "娱乐") == 0


def test_resolve_blank_or_none_category(db):
    for value in (None, "", "   "):
        assert resolve_category_id(db, "create_task", value) == 0


def test_resolve_soft_deleted_category_returns_zero(db):
    db.execute(
        text("UPDATE categories SET deleted_at = NOW() WHERE id = :i"), {"i": TASK_WORK_ID}
    )
    db.commit()

    assert resolve_category_id(db, "create_task", "工作") == 0


def test_resolve_unknown_action_type_returns_zero(db):
    assert resolve_category_id(db, "delete_everything", "工作") == 0


# ---------- 可读文本 ----------


def test_format_due_text_beijing_with_weekday():
    # 2026-11-09 是周一
    assert format_due_text("2026-11-09T15:00:00+08:00") == "2026-11-09 15:00（周一）"


def test_format_due_text_converts_utc_to_beijing():
    """+00:00 的 07:00 就是北京时间 15:00 —— 直接切字符串会差 8 小时。"""
    assert format_due_text("2026-11-09T07:00:00+00:00").startswith("2026-11-09 15:00")


def test_format_due_text_handles_z_suffix():
    assert format_due_text("2026-11-09T07:00:00Z").startswith("2026-11-09 15:00")


def test_format_due_text_drops_weekday_beyond_a_week():
    assert format_due_text("2027-02-01T09:00:00+08:00") == "2027-02-01 09:00"


def test_format_due_text_none_and_garbage():
    assert format_due_text(None) is None
    assert format_due_text("不是时间") is None


def test_build_preview_adds_text_fields():
    preview = build_preview(
        "create_task",
        {"title": "去超市", "due_at": "2026-11-09T15:00:00+08:00", "priority": "high"},
    )

    assert preview["due_at_text"] == "2026-11-09 15:00（周一）"
    assert preview["priority_text"] == "高"
    assert preview["title"] == "去超市"


def test_build_preview_without_due_at():
    preview = build_preview("create_task", {"title": "买牛奶"})

    assert preview["due_at_text"] is None


def test_build_preview_does_not_mutate_input():
    payload = {"title": "去超市", "priority": "low"}
    build_preview("create_task", payload)

    assert payload == {"title": "去超市", "priority": "low"}


# ---------- 创建 ----------


def test_create_draft_writes_pending_row(db):
    session = _make_session_row(db)
    message = _make_assistant_message(db, session.id)
    draft = _draft('{"title":"去超市","due_at":"2026-11-09T15:00:00+08:00","priority":"high","category_name":"购物"}')

    action = create_draft(db, user_id=1, session=session, message_id=message.id, draft=draft)
    db.commit()

    assert action.id > 0
    assert action.status == "pending"
    assert action.action_type == "create_task"
    assert action.message_id == message.id
    assert uuid.UUID(action.idempotency_key)  # 是合法 UUID
    delta = action.expires_at - now_utc()
    # 允许一点点溢出：CPU 时钟与 wall clock 有漂移，expires_at 是用 wall clock 算的
    assert timedelta(minutes=DEFAULT_TTL_MINUTES - 1) < delta <= timedelta(minutes=DEFAULT_TTL_MINUTES + 1)


def test_create_draft_fills_category_id_and_preview(db):
    session = _make_session_row(db)
    message = _make_assistant_message(db, session.id)
    draft = _draft('{"title":"去超市","category_name":"购物","priority":"medium"}')

    action = create_draft(db, user_id=1, session=session, message_id=message.id, draft=draft)
    db.commit()

    assert action.payload["category_id"] == TASK_SHOPPING_ID
    assert action.payload["category_name"] == "购物"
    assert action.payload["priority_text"] == "中"


def test_create_draft_unknown_category_still_succeeds(db):
    session = _make_session_row(db)
    message = _make_assistant_message(db, session.id)
    draft = _draft('{"title":"去超市","category_name":"不存在的分类"}')

    action = create_draft(db, user_id=1, session=session, message_id=message.id, draft=draft)
    db.commit()

    assert action.payload["category_id"] == 0


def test_create_draft_does_not_commit(db):
    """service 需要把「消息 + 计数 + 草稿」一起提交，所以这里不能自己 commit。"""
    session = _make_session_row(db)
    message = _make_assistant_message(db, session.id)
    draft = _draft('{"title":"去超市"}')

    action = create_draft(db, user_id=1, session=session, message_id=message.id, draft=draft)

    with new_session() as other:
        assert other.scalar(select(func.count()).select_from(PendingAction)) == 0
    assert action.id > 0  # flush 拿到了自增 id

    db.commit()
    with new_session() as other:
        assert other.scalar(select(func.count()).select_from(PendingAction)) == 1


def test_create_draft_twice_makes_two_rows(db):
    """用户重复发同一句话，本来就该得到两张卡（幂等只约束「确认」那一下）。"""
    session = _make_session_row(db)
    message = _make_assistant_message(db, session.id)
    draft = _draft('{"title":"去超市"}')

    first = create_draft(db, user_id=1, session=session, message_id=message.id, draft=draft)
    second = create_draft(db, user_id=1, session=session, message_id=message.id, draft=draft)
    db.commit()

    assert first.id != second.id
    assert first.idempotency_key != second.idempotency_key


def test_create_draft_writes_no_business_data(db):
    """整个 A5 的安全底线：确认之前，待办表一行都不该有。"""
    session = _make_session_row(db)
    message = _make_assistant_message(db, session.id)
    draft = _draft('{"title":"去超市"}')

    create_draft(db, user_id=1, session=session, message_id=message.id, draft=draft)
    db.commit()

    with new_session() as other:
        assert other.scalar(select(func.count()).select_from(Task)) == 0


# ---------- 读取 ----------


def test_get_pending_returns_own_action(db):
    action = _seed_pending(db)

    assert get_pending(db, 1, action.id).id == action.id


def test_get_pending_other_users_action_is_404(db):
    action = _seed_pending(db, user_id=2)

    with pytest.raises(AppError) as exc:
        get_pending(db, 1, action.id)

    assert exc.value.status_code == 404
    assert exc.value.code == "NOT_FOUND"


def test_get_pending_missing_is_404(db):
    with pytest.raises(AppError) as exc:
        get_pending(db, 1, 999)

    assert exc.value.status_code == 404


def test_get_pending_expired_sets_status_and_raises_410(db):
    action = _seed_pending(db)
    action.expires_at = now_utc() - timedelta(minutes=1)
    db.commit()

    with pytest.raises(AppError) as exc:
        get_pending(db, 1, action.id)

    assert exc.value.status_code == 410
    assert exc.value.code == "ACTION_EXPIRED"
    with new_session() as other:
        assert other.get(PendingAction, action.id).status == "expired"


def test_get_pending_confirmed_action_is_readable(db):
    """已确认的也能读：前端刷新时要把卡片画成「已创建」，不能 404。"""
    action = _seed_pending(db, status="confirmed")

    assert get_pending(db, 1, action.id).status == "confirmed"


# ---------- 状态流转 ----------


def test_mark_confirmed_then_confirm_again_fails(db):
    action = _seed_pending(db)

    assert mark_confirmed(db, action.id, result={"task_id": 12}) is True
    assert mark_confirmed(db, action.id, result={"task_id": 99}) is False

    with new_session() as other:
        row = other.get(PendingAction, action.id)
        assert row.status == "confirmed"
        assert row.executed_at is not None
        assert row.result == {"task_id": 12}  # 第二次没有覆盖第一次的结果


def test_mark_confirmed_on_cancelled_returns_false(db):
    action = _seed_pending(db)
    assert mark_cancelled(db, action.id) is True

    assert mark_confirmed(db, action.id, result={"task_id": 1}) is False


def test_mark_cancelled_twice_returns_false_second_time(db):
    action = _seed_pending(db)

    assert mark_cancelled(db, action.id) is True
    assert mark_cancelled(db, action.id) is False


def test_mark_confirmed_on_expired_returns_false(db):
    """已过期但还没被置位（status 仍是 pending）的动作也确认不了。

    时间条件写在 UPDATE 的 WHERE 里 —— 不是靠 get_pending 先读过一遍。
    """
    action = _seed_pending(db)
    action.expires_at = now_utc() - timedelta(minutes=5)
    db.commit()

    assert mark_confirmed(db, action.id, result={"task_id": 1}) is False
    with new_session() as other:
        assert other.get(PendingAction, action.id).status == "pending"


def test_mark_cancelled_on_expired_returns_false(db):
    action = _seed_pending(db)
    action.expires_at = now_utc() - timedelta(minutes=5)
    db.commit()

    assert mark_cancelled(db, action.id) is False


def test_only_pending_is_actionable(db):
    """一次能跑完的状态矩阵：只有 pending 能被改成 confirmed / cancelled。"""
    for status in ("confirmed", "cancelled", "expired"):
        action = _seed_pending(db, status=status)
        assert mark_confirmed(db, action.id, result={}) is False
        assert mark_cancelled(db, action.id) is False


# ---------- 时间口径 ----------


def test_expires_at_is_naive_utc(db):
    """库里全是 naive UTC；写进带时区的时间会被 MySQL 静默转换，差 8 小时。"""
    session = _make_session_row(db)
    message = _make_assistant_message(db, session.id)
    draft = _draft('{"title":"去超市"}')

    action = create_draft(db, user_id=1, session=session, message_id=message.id, draft=draft)
    db.commit()

    assert action.expires_at.tzinfo is None  # 口径：naive UTC，不是带偏移的 datetime


# 第 3 步没加接口；这一行是提醒：别顺手在 pending.py 里加 router