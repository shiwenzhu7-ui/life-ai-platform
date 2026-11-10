"""B6：跨用户数据隔离（V1.2 验收项「两用户互不可见」）。

conftest 把「当前用户」钉死成 1 号，本文件用 `acting_as(2)` 冒充 2 号用户，
证明 2 号用户对 1 号用户的数据**读不到、改不动、删不掉**，1 号也看不见 2 号的。

四类断言贯穿所有模块：
    列表   别人的数据不出现在我的列表里
    详情   按 id 直接取 → 404（不是 403：403 等于承认「这条存在」）
    修改   PATCH 别人的 → 404，且原数据一个字都没变
    删除   DELETE 别人的 → 404，且原数据还在

例外只有一处：分类字典里的**系统预置分类**（owner_user_id=0）两边都看得到 ——
它不属于任何人，这就是它存在的意义。
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from conftest import API, engine, make_expense, make_task, new_session

from app import app
from core.deps import get_current_user_id
from core.time import now_utc
from modules.lifeweb.ai.models import ChatMessage, ChatSession, PendingAction
from modules.lifeweb.expenses.model import Expense
from modules.shared.categories.model import Category
from modules.shared.users.model import User

ME, OTHER = 1, 2


@contextmanager
def acting_as(user_id: int):
    """临时冒充某个用户。退出时务必还原成 1 号 —— 否则后面所有用例都会变成别人的身份。"""
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    try:
        yield
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: ME


@pytest.fixture()
def two_users():
    """1 号和 2 号用户。业务表里的 user_id 至少要有一个真实存在的行。"""
    with new_session() as s:
        s.add_all(
            [
                User(id=ME, username="me", nickname="我", role="user", status="active"),
                User(id=OTHER, username="other", nickname="他", role="user", status="active"),
            ]
        )
        s.commit()
    yield ME, OTHER


@pytest.fixture()
def fake_llm():
    """把模型换成 FakeProvider：隔离测试不该因为「模型回什么」忽红忽绿。"""
    from modules.lifeweb.ai.provider import FakeProvider, LLMReply
    from modules.lifeweb.ai.router import get_provider

    # 预置几条回复：本文件的用例一共要问 3 次（1 号 1 次、2 号 2 次），
    # 不给的话 FakeProvider 会抛 FAKE_EXHAUSTED 变成 500
    fake = FakeProvider([LLMReply(content="好的"), LLMReply(content="好的"), LLMReply(content="好的")])
    app.dependency_overrides[get_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_provider, None)


def _session() -> Session:
    return Session(engine)


def _make_expense(user_id: int, amount: str = "10.00", note: str | None = None) -> int:
    """造一笔「此刻发生」的账。occurred_at 是 NOT NULL，必须显式给。"""
    with new_session() as s:
        row = make_expense(s, user_id=user_id, amount=amount, note=note, occurred_at=now_utc())
        return row.id


def _make_chat_session(user_id: int, title: str = "会话") -> int:
    with new_session() as s:
        session = ChatSession(user_id=user_id, title=title, model="fake")
        s.add(session)
        s.commit()
        return session.id


def _make_pending_action(user_id: int, session_id: int) -> int:
    """造一张待确认卡片（status=pending、30 分钟内有效），不经过模型。"""
    with new_session() as s:
        message = ChatMessage(
            user_id=user_id, session_id=session_id, role="assistant", content="草稿"
        )
        s.add(message)
        s.flush()
        action = PendingAction(
            user_id=user_id,
            session_id=session_id,
            message_id=message.id,
            action_type="create_task",
            payload={"title": "别人的草稿", "priority": "medium"},
            status="pending",
            idempotency_key=str(uuid.uuid4()),
            expires_at=now_utc() + timedelta(minutes=30),
        )
        s.add(action)
        s.commit()
        return action.id


# ---------- 待办 ----------


def test_tasks_list_isolation(client, two_users):
    with _session() as s:
        make_task(s, user_id=ME, title="我的待办")
        make_task(s, user_id=OTHER, title="他的待办")

    mine = client.get(f"{API}/tasks").json()
    assert mine["total"] == 1
    assert [i["title"] for i in mine["items"]] == ["我的待办"]

    with acting_as(OTHER):
        theirs = client.get(f"{API}/tasks").json()
    assert theirs["total"] == 1
    assert [i["title"] for i in theirs["items"]] == ["他的待办"]


def test_tasks_get_patch_delete_are_404_for_other(client, two_users):
    with _session() as s:
        task = make_task(s, user_id=ME, title="只有我能看")
        task_id = task.id

    with acting_as(OTHER):
        assert client.get(f"{API}/tasks/{task_id}").status_code == 404
        r = client.patch(f"{API}/tasks/{task_id}", json={"title": "被改了"})
        assert r.status_code == 404
        r = client.patch(f"{API}/tasks/{task_id}/status", json={"status": "done"})
        assert r.status_code == 404
        assert client.delete(f"{API}/tasks/{task_id}").status_code == 404

    # 原数据一个字都没变：标题还是原名、状态还是 todo、没被软删
    with new_session() as s:
        row = s.get(type(task), task_id)
        assert row.title == "只有我能看"
        assert row.status == "todo"
        assert row.deleted_at is None


def test_task_detail_is_not_shared_when_both_create_same_title(client, two_users):
    """两边都建了同名待办时，各自只能看到自己那一条（防止按 title 串了）。"""
    with _session() as s:
        mine = make_task(s, user_id=ME, title="买牛奶")
        theirs = make_task(s, user_id=OTHER, title="买牛奶")
        mine_id, theirs_id = mine.id, theirs.id

    assert client.get(f"{API}/tasks/{mine_id}").json()["id"] == mine_id
    with acting_as(OTHER):
        assert client.get(f"{API}/tasks/{theirs_id}").json()["id"] == theirs_id
        assert client.get(f"{API}/tasks/{mine_id}").status_code == 404


# ---------- 消费 ----------


def test_expenses_list_isolation(client, two_users):
    _make_expense(ME, amount="10.00", note="我的")
    _make_expense(OTHER, amount="99.00", note="他的")

    mine = client.get(f"{API}/expenses").json()
    assert mine["total"] == 1
    assert mine["items"][0]["note"] == "我的"

    with acting_as(OTHER):
        theirs = client.get(f"{API}/expenses").json()
    assert theirs["total"] == 1
    assert theirs["items"][0]["note"] == "他的"


def test_expenses_get_patch_delete_are_404_for_other(client, two_users):
    expense_id = _make_expense(ME, amount="10.00", note="我的账")

    with acting_as(OTHER):
        assert client.get(f"{API}/expenses/{expense_id}").status_code == 404
        assert client.patch(f"{API}/expenses/{expense_id}", json={"amount": "1.00"}).status_code == 404
        assert client.delete(f"{API}/expenses/{expense_id}").status_code == 404

    with new_session() as s:
        row = s.get(Expense, expense_id)
        assert str(row.amount).startswith("10")
        assert row.deleted_at is None


def test_expenses_summary_only_counts_own(client, two_users):
    """月度汇总的金额只算自己的：2 号用户的钱不能出现在 1 号的数字里。"""
    _make_expense(ME, amount="10.00")
    _make_expense(OTHER, amount="99.00")

    body = client.get(f"{API}/expenses/summary").json()
    assert body["expense_total"] == "10.00"

    with acting_as(OTHER):
        other = client.get(f"{API}/expenses/summary").json()
    assert other["expense_total"] == "99.00"


# ---------- 首页 ----------


def test_dashboard_only_counts_own_data(client, two_users):
    with _session() as s:
        make_task(s, user_id=ME, title="我的")
        make_task(s, user_id=OTHER, title="他的 A")
        make_task(s, user_id=OTHER, title="他的 B")
    _make_expense(ME, amount="10.00")
    _make_expense(OTHER, amount="99.00")

    body = client.get(f"{API}/dashboard/overview").json()
    assert body["tasks"]["todo"] == 1
    assert body["expense"]["today_total"] == "10.00"

    with acting_as(OTHER):
        other = client.get(f"{API}/dashboard/overview").json()
    assert other["tasks"]["todo"] == 2
    assert other["expense"]["today_total"] == "99.00"


# ---------- 分类 ----------


def test_categories_system_visible_to_all_but_custom_private(client, two_users):
    """系统分类（owner=0）两边都看得到；各自自建的只有自己能看。"""
    with new_session() as s:
        s.add_all(
            [
                Category(
                    scope="task",
                    name="我的私分类",
                    parent_id=0,
                    owner_user_id=ME,
                    is_system=0,
                    sort_order=1,
                ),
                Category(
                    scope="task",
                    name="他的私分类",
                    parent_id=0,
                    owner_user_id=OTHER,
                    is_system=0,
                    sort_order=1,
                ),
            ]
        )
        s.commit()

    mine = {c["name"] for c in client.get(f"{API}/categories?scope=task").json()}
    assert "我的私分类" in mine
    assert "他的私分类" not in mine
    assert "工作" in mine, "系统预置分类人人可见"

    with acting_as(OTHER):
        theirs = {c["name"] for c in client.get(f"{API}/categories?scope=task").json()}
    assert "他的私分类" in theirs
    assert "我的私分类" not in theirs


def test_cannot_use_other_users_category_on_own_task(two_users):
    """别人的私有分类 ID 不能塞进自己的待办 —— 否则能借 422/201 的差别试探它是否存在。"""
    from fastapi.testclient import TestClient

    with new_session() as s:
        theirs = Category(
            scope="task",
            name="他的私分类",
            parent_id=0,
            owner_user_id=OTHER,
            is_system=0,
            sort_order=1,
        )
        s.add(theirs)
        s.commit()
        category_id = theirs.id

    with TestClient(app) as c:
        r = c.post(
            f"{API}/tasks", json={"title": "借别人的分类", "category_id": category_id}
        )
    assert r.status_code == 422


# ---------- AI 会话 ----------


def test_ai_sessions_list_isolation(client, two_users):
    _make_chat_session(ME, "我的会话")
    _make_chat_session(OTHER, "他的会话")

    mine = client.get(f"{API}/ai/sessions").json()
    assert [i["title"] for i in mine["items"]] == ["我的会话"]

    with acting_as(OTHER):
        theirs = client.get(f"{API}/ai/sessions").json()
    assert [i["title"] for i in theirs["items"]] == ["他的会话"]


def test_ai_session_detail_messages_patch_delete_are_404_for_other(client, two_users):
    session_id = _make_chat_session(ME, "只有我能看")

    with acting_as(OTHER):
        assert client.get(f"{API}/ai/sessions/{session_id}").status_code == 404
        assert client.get(f"{API}/ai/sessions/{session_id}/messages").status_code == 404
        assert (
            client.patch(f"{API}/ai/sessions/{session_id}", json={"title": "被改了"}).status_code
            == 404
        )
        assert client.delete(f"{API}/ai/sessions/{session_id}").status_code == 404

    with new_session() as s:
        row = s.get(ChatSession, session_id)
        assert row.title == "只有我能看"
        assert row.deleted_at is None


def test_ai_chat_isolation(client, two_users, fake_llm):
    """1 号先聊一句；2 号再开聊，绝不能被拉进 1 号的会话。"""
    r1 = client.post(f"{API}/ai/chat", json={"message": "你好"})
    assert r1.status_code == 200, r1.text
    my_session = r1.json()["session_id"]

    with acting_as(OTHER):
        r2 = client.post(f"{API}/ai/chat", json={"message": "你好"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["session_id"] != my_session, "2 号必须另开会话"

        # 显式指定别人的会话 id：不能续聊进去
        r3 = client.post(
            f"{API}/ai/chat", json={"message": "接着聊", "session_id": my_session}
        )
        assert r3.status_code == 404


def test_pending_action_confirm_and_cancel_are_404_for_other(client, two_users):
    session_id = _make_chat_session(ME)
    action_id = _make_pending_action(ME, session_id)

    with acting_as(OTHER):
        assert client.post(f"{API}/ai/actions/{action_id}/confirm").status_code == 404
        assert client.post(f"{API}/ai/actions/{action_id}/cancel").status_code == 404

    # 卡片状态没被改，也没替 2 号用户建出待办
    with new_session() as s:
        assert s.get(PendingAction, action_id).status == "pending"
    from modules.lifeweb.tasks.model import Task

    with new_session() as s:
        assert s.query(Task).filter_by(user_id=OTHER).count() == 0


# ---------- /auth/me ----------


def test_me_shows_own_identity(client, two_users):
    assert client.get(f"{API}/auth/me").json()["username"] == "me"
    with acting_as(OTHER):
        assert client.get(f"{API}/auth/me").json()["username"] == "other"