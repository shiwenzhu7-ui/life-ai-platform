"""A7：`update_task` 改待办 —— 模型第二次直接碰业务表的机会。

A5 证明了「新建」的边界，A6 证明了「读」的边界，这边证明**「改」的边界**。
改比新建更危险：新建错了多一条，改错了是**原来那条被改没了**。所以测试的重心
不在于「能改成功」，而在于下面这些「不该发生的事」：

1. 模型编一个 task_id 出来 —— 必须 404，而不是改到别人头上或者报 500
2. 只改标题时，时间 / 优先级 / 分类**一个都不许动**（这是 `before` 打底的意义）
3. 卡片上必须带着「改之前长什么样」，否则用户没有判断依据就点了确认
4. 用户点了确认之后，改的是**一条**待办：不多出一条，也不改错一条
5. 重复确认 / 过期 / 取消 —— 与新建同一套状态机，一次都不能多改

全部 FakeProvider + 进程内 TestClient，不联网不花钱。
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import API, make_task, new_session

from app import app
from core.time import now_utc
from modules.lifeweb.ai.models import ChatSession, PendingAction
from modules.lifeweb.ai.pending import CODE_ALREADY_DONE, prepare_payload
from modules.lifeweb.ai.provider import FakeProvider, LLMReply
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.tasks.model import Task

CST = timezone(timedelta(hours=8))
# conftest 的种子分类 id 固定：15 = task/工作，18 = task/购物
TASK_WORK_ID = 15
TASK_SHOPPING_ID = 18


# ---------- 夹具 ----------


def _call(**arguments) -> dict:
    return {
        "id": "call_update",
        "type": "function",
        "name": "update_task",
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


def _chat(client, message: str = "把那条的时间改到明天下午三点"):
    return client.post(f"{API}/ai/chat", json={"message": message})


def _reload_task(task_id: int) -> Task:
    with new_session() as s:
        return s.get(Task, task_id)


def _task_count() -> int:
    with new_session() as s:
        return s.query(Task).count()


def _seed_pending(db, task_id: int, *, status: str = "pending", minutes: int = 30, **changes):
    """造一张「改待办」的草稿，payload 结构与 create_draft 产出的一致。"""
    before = {
        "task_id": task_id,
        "title": "原来的标题",
        "due_at": "2026-11-20T09:00:00+08:00",
        "priority": "low",
        "category_id": TASK_WORK_ID,
        "category_name": "工作",
    }
    data = {**before, **changes, "before": before}
    action = PendingAction(
        user_id=1,
        session_id=1,
        message_id=1,
        action_type="update_task",
        payload=data,
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


# ---------- 出卡：卡片上要有「改之前」 ----------


def test_chat_produces_update_card_with_before(client, provider, db):
    """卡片必须同时带着 before 和改动项 —— 只有改动项，用户不知道改的是哪条。"""
    task = make_task(
        db,
        title="去银行办卡",
        priority="low",
        category_id=TASK_WORK_ID,
        due_at=datetime(2026, 11, 13, 6, 0),
    )
    provider.queue(
        _reply(
            calls=[
                _call(
                    task_id=task.id,
                    title="去银行办储蓄卡",
                    due_at="2026-11-15T15:00:00+08:00",
                    priority="high",
                )
            ]
        )
    )

    res = _chat(client, "把去银行办卡那条改个名，时间挪到后天下午三点，优先级调高")

    assert res.status_code == 200, res.text
    body = res.json()
    card = body["pending_action"]
    assert card is not None, "改待办也要出确认卡，不能直接改"
    assert card["action_type"] == "update_task"

    preview = card["preview"]
    assert preview["title"] == "去银行办储蓄卡"
    assert preview["priority"] == "high"
    assert preview["due_at_text"] == "2026-11-15 15:00（周日）"
    before = preview["before"]
    assert before["task_id"] == task.id
    assert before["title"] == "去银行办卡"
    assert before["priority"] == "low"
    # 原时间入库时被转成 naive UTC 06:00，预览要还原成北京时间 14:00
    assert before["due_at"] == "2026-11-13T14:00:00+08:00"
    assert before["category_id"] == TASK_WORK_ID


def test_chat_card_is_only_a_draft(client, provider, db):
    """出卡那一刻，业务表一个字都不能变。"""
    task = make_task(db, title="买菜", priority="low")
    provider.queue(_reply(calls=[_call(task_id=task.id, title="买菜和水果")]))

    assert _chat(client).status_code == 200

    assert _reload_task(task.id).title == "买菜", "用户还没点确认，怎么就已经改了"


def test_unknown_task_id_is_404_and_no_card(client, provider, db):
    """模型编了一个不存在的 id：这一轮直接失败，不落卡片。"""
    make_task(db, title="买菜")
    provider.queue(_reply(calls=[_call(task_id=99999, title="瞎编的")]))
    # 重试也还是假 id
    provider.queue(_reply(calls=[_call(task_id=99999, title="瞎编的")]))

    res = _chat(client)

    assert res.status_code == 404, res.text
    assert "不存在" in res.json()["message"]
    with new_session() as s:
        assert s.query(PendingAction).count() == 0


def test_other_users_task_is_404(client, provider, db):
    """别人的待办：对模型来说等同于不存在，不能借此改到别人头上。"""
    other = make_task(db, user_id=2, title="别人的待办")
    provider.queue(_reply(calls=[_call(task_id=other.id, title="被我改了")]))
    provider.queue(_reply(calls=[_call(task_id=other.id, title="被我改了")]))

    res = _chat(client)

    assert res.status_code == 404
    assert _reload_task(other.id).title == "别人的待办"


def test_deleted_task_is_404(client, provider, db):
    """删掉的待办也不能改 —— 行已经不在了。"""
    task = make_task(db, title="要删掉的")
    client.delete(f"{API}/tasks/{task.id}")
    provider.queue(_reply(calls=[_call(task_id=task.id, title="改一改")]))
    provider.queue(_reply(calls=[_call(task_id=task.id, title="改一改")]))

    assert _chat(client).status_code == 404


# ---------- 确认：真的改，且只改一条 ----------


def test_confirm_updates_exactly_one_row(client, db):
    task = make_task(db, title="原来的标题", priority="low", category_id=TASK_WORK_ID)
    action = _seed_pending(db, task.id, title="新标题")

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "confirmed"
    assert body["created"] == {"type": "task", "id": task.id}

    row = _reload_task(task.id)
    assert row.title == "新标题"
    assert _task_count() == 1, "改待办不能多出一条"


def test_confirm_keeps_untouched_fields(client, db):
    """草稿只改了标题：时间、优先级、分类必须原封不动。

    这是 `before` 打底的全部意义。要是这里退化成「没说的字段置空」，
    用户说「改个名字」，待办的截止时间就没了 —— 而这种损失很难被察觉。
    """
    task = make_task(
        db,
        title="原来的标题",
        priority="low",
        category_id=TASK_WORK_ID,
        due_at=datetime(2026, 11, 20, 1, 0),
    )
    action = _seed_pending(db, task.id, title="新标题")

    assert _confirm(client, action.id).status_code == 200

    row = _reload_task(task.id)
    assert row.title == "新标题"
    assert row.priority == "low"
    assert row.category_id == TASK_WORK_ID
    assert row.due_at == datetime(2026, 11, 20, 1, 0), "改标题不该顺手清掉截止时间"


def test_confirm_applies_due_at_and_category(client, db):
    task = make_task(db, title="原来的标题", priority="low", category_id=TASK_WORK_ID)
    action = _seed_pending(
        db,
        task.id,
        due_at="2026-11-15T15:00:00+08:00",
        category_name="购物",
        category_id=TASK_SHOPPING_ID,
    )

    assert _confirm(client, action.id).status_code == 200

    row = _reload_task(task.id)
    # 15:00 +08:00 → 07:00 UTC（库里存 naive UTC）
    assert row.due_at == datetime(2026, 11, 15, 7, 0)
    assert row.category_id == TASK_SHOPPING_ID


def test_update_keeps_original_category_when_not_mentioned(db):
    """没说分类就沿用原值，而不是沿用模型那次给的值（那可能是另一个域的分类）。"""
    task = make_task(db, title="原来的标题", category_id=TASK_WORK_ID)
    action = _seed_pending(db, task.id, title="新标题")

    data = prepare_payload(db, action, None)

    assert data["category_id"] == TASK_WORK_ID


def test_update_recategorizes_when_user_changes_it(db):
    """用户在前端把分类改成「购物」：以新名字为准重新查 id。"""
    task = make_task(db, title="原来的标题", category_id=TASK_WORK_ID)
    action = _seed_pending(db, task.id, title="新标题")

    data = prepare_payload(db, action, {"category_name": "购物"})

    assert data["category_id"] == TASK_SHOPPING_ID


def test_confirm_ignores_fields_outside_whitelist(client, db):
    """user_id / status / id 这些不是「内容」，override 里写了也不许生效。"""
    task = make_task(db, title="原来的标题")
    action = _seed_pending(db, task.id, title="新标题")

    res = _confirm(
        client,
        action.id,
        {
            "payload_override": {
                "title": "新标题",
                "user_id": 999,
                "status": "done",
                "id": 12345,
            }
        },
    )

    assert res.status_code == 200, res.text
    assert _reload_task(task.id).user_id == 1
    assert _reload_task(task.id).status == "todo"


def test_confirm_with_bad_override_keeps_card_pending(client, db):
    """用户改出个空标题：422，卡片还留着（能再改一次）。"""
    task = make_task(db, title="原来的标题")
    action = _seed_pending(db, task.id, title="新标题")

    res = _confirm(client, action.id, {"payload_override": {"title": "   "}})

    assert res.status_code == 422, res.text
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"
    assert _reload_task(task.id).title == "原来的标题"


# ---------- 状态机：一次都不能多改 ----------


def test_second_confirm_is_409_and_changes_nothing(client, db):
    task = make_task(db, title="原来的标题")
    action = _seed_pending(db, task.id, title="第一次改的")

    assert _confirm(client, action.id).status_code == 200
    second = _confirm(client, action.id, {"payload_override": {"title": "第二次改的"}})

    assert second.status_code == 409
    assert second.json()["code"] == CODE_ALREADY_DONE
    assert _reload_task(task.id).title == "第一次改的"
    assert _task_count() == 1


def test_expired_update_card_is_410(client, db):
    task = make_task(db, title="原来的标题")
    action = _seed_pending(db, task.id, title="新标题", minutes=-1)

    res = _confirm(client, action.id)

    assert res.status_code == 410
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "expired"
    assert _reload_task(task.id).title == "原来的标题"


def test_cancelled_update_card_cannot_confirm(client, db):
    task = make_task(db, title="原来的标题")
    action = _seed_pending(db, task.id, title="新标题")
    assert client.post(f"{API}/ai/actions/{action.id}/cancel", json={}).status_code == 200

    res = _confirm(client, action.id)

    assert res.status_code == 409
    assert _reload_task(task.id).title == "原来的标题"


def test_update_writes_nothing_on_failure(client, db, monkeypatch):
    """业务写失败时卡片必须原地不动 —— 不能出现「卡片已确认、待办没改成」。"""
    from modules.lifeweb.ai import execute

    task = make_task(db, title="原来的标题")
    action = _seed_pending(db, task.id, title="新标题")

    def _boom(*args, **kwargs):
        raise RuntimeError("数据库炸了")

    monkeypatch.setitem(execute._WRITERS, "update_task", _boom)

    with pytest.raises(RuntimeError):
        _confirm(client, action.id)

    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"
    assert _reload_task(task.id).title == "原来的标题"


# ---------- 兜底正文 ----------


def test_empty_content_says_update_not_create(client, provider, db):
    """模型交了工具却一个字不说时，兜底正文不能是「我整理了一条待办」——
    那是新建的说法，用户会以为多出来了一条。"""
    task = make_task(db, title="买菜")
    provider.queue(_reply(content=None, calls=[_call(task_id=task.id, title="买菜和水果")]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    assert res.json()["reply"] == "我整理了一条修改，请你确认。"