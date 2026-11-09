"""A10：`delete_task` 删待办 —— AI 侧第一个「不可逆」的动作。

改错还能再改回来，删错只能去库里捞。所以这份测试的重心几乎全在
「不该发生的事」上，而不是「能删成功」：

1. 出卡那一刻不能删：用户还没看见卡片，业务表一个字都不许动
2. 卡片上必须带着**要删的那条待办长什么样**（`gone` 快照）——
   否则「删买菜」这张卡和「删交房租」长得一样，用户没有判断依据
3. 模型编 id / 抄到别人的 id / 抄到已软删的 —— 一律 404，不落卡片
4. 用户确认后：只软删那一条，`deleted_at` 有值、行还在（不是 DELETE）
5. 重复确认、过期、取消，一次都不能多删
6. override 里塞别的字段（换个 id 删、改成删会话）一律挡下

全部 FakeProvider + 进程内 TestClient，不联网不花钱。
"""

import json
import uuid
from datetime import datetime, timedelta

import pytest

from conftest import API, make_task, new_session

from app import app
from core.time import now_utc
from modules.lifeweb.ai.models import PendingAction
from modules.lifeweb.ai.pending import CODE_ALREADY_DONE, prepare_payload
from modules.lifeweb.ai.provider import FakeProvider, LLMReply
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.tasks.model import Task

# conftest 的种子分类 id 固定：15 = task/工作
TASK_WORK_ID = 15


# ---------- 夹具 ----------


def _call(**arguments) -> dict:
    return {
        "id": "call_delete",
        "type": "function",
        "name": "delete_task",
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


def _chat(client, message: str = "把买菜那条删了"):
    return client.post(f"{API}/ai/chat", json={"message": message})


def _reload_task(task_id: int) -> Task:
    with new_session() as s:
        return s.get(Task, task_id)


def _is_deleted(task_id: int) -> bool:
    """用新会话查，绕开 REPEATABLE READ 的旧快照。"""
    with new_session() as s:
        return s.get(Task, task_id).deleted_at is not None


def _pending_count() -> int:
    with new_session() as s:
        return s.query(PendingAction).count()


def _seed_pending(db, task_id: int, *, status: str = "pending", minutes: int = 30, **extra):
    """造一张「删待办」的草稿，payload 结构与 create_draft 产出的一致。"""
    gone = {
        "task_id": task_id,
        "title": "买菜",
        "due_at": "2026-11-20T09:00:00+08:00",
        "priority": "medium",
        "category_id": TASK_WORK_ID,
    }
    action = PendingAction(
        user_id=1,
        session_id=1,
        message_id=1,
        action_type="delete_task",
        payload={"task_id": task_id, "reason": "买完了", "gone": gone, **extra},
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


# ---------- 出卡：卡片上要有「要删的是哪条」 ----------


def test_chat_produces_delete_card_with_snapshot(client, provider, db):
    """卡片必须带着待办的快照 —— 光有 task_id，用户没法核对删的是哪条。"""
    task = make_task(
        db,
        title="买菜",
        priority="low",
        category_id=TASK_WORK_ID,
        due_at=datetime(2026, 11, 13, 6, 0),
    )
    provider.queue(_reply(calls=[_call(task_id=task.id, reason="买完了")]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    card = res.json()["pending_action"]
    assert card is not None, "删除必须出确认卡，不能直接删"
    assert card["action_type"] == "delete_task"

    preview = card["preview"]
    assert preview["task_id"] == task.id
    assert preview["reason"] == "买完了"
    gone = preview["gone"]
    assert gone["title"] == "买菜"
    assert gone["priority"] == "low"
    # 入库时是 naive UTC 06:00，快照要还原成北京时间 14:00
    assert gone["due_at"] == "2026-11-13T14:00:00+08:00"
    assert gone["due_at_text"] == "2026-11-13 14:00（周五）"
    assert gone["priority_text"] == "低"
    assert gone["category_id"] == TASK_WORK_ID


def test_chat_card_is_only_a_draft(client, provider, db):
    """出卡那一刻，待办必须原封不动。"""
    task = make_task(db, title="买菜")
    provider.queue(_reply(calls=[_call(task_id=task.id)]))

    assert _chat(client).status_code == 200

    assert not _is_deleted(task.id), "用户还没点确认，怎么就已经删了"


def test_reason_is_optional(client, provider, db):
    """用户没说为什么删，卡片照样出，reason 为 None。"""
    task = make_task(db, title="买菜")
    provider.queue(_reply(calls=[_call(task_id=task.id)]))

    res = _chat(client, "这条删了吧")

    assert res.status_code == 200, res.text
    assert res.json()["pending_action"]["preview"]["reason"] is None


def test_unknown_task_id_is_404_and_no_card(client, provider, db):
    """模型编了一个不存在的 id：这一轮失败，不落卡片。"""
    make_task(db, title="买菜")
    provider.queue(_reply(calls=[_call(task_id=99999)]))
    provider.queue(_reply(calls=[_call(task_id=99999)]))  # 重试还是假 id

    res = _chat(client)

    assert res.status_code == 404, res.text
    assert "不存在" in res.json()["message"]
    assert _pending_count() == 0


def test_other_users_task_is_404(client, provider, db):
    """别人的待办：对模型来说等同于不存在。"""
    other = make_task(db, user_id=2, title="别人的待办")
    provider.queue(_reply(calls=[_call(task_id=other.id)]))
    provider.queue(_reply(calls=[_call(task_id=other.id)]))

    assert _chat(client).status_code == 404
    assert not _is_deleted(other.id)


def test_soft_deleted_task_is_404(client, provider, db):
    """已经软删的再删一次：404，不建一张注定点不动的卡。"""
    task = make_task(db, title="早删过了")
    with new_session() as s:
        s.get(Task, task.id).deleted_at = now_utc()
        s.commit()
    provider.queue(_reply(calls=[_call(task_id=task.id)]))
    provider.queue(_reply(calls=[_call(task_id=task.id)]))

    assert _chat(client).status_code == 404


def test_missing_task_id_is_rejected(client, provider, db):
    """模型只说了「删掉」却不给 id：参数校验失败，走重试，仍失败则 502。"""
    make_task(db, title="买菜")
    provider.queue(_reply(calls=[_call(reason="用户让删的")]))
    provider.queue(_reply(calls=[_call(reason="用户让删的")]))

    res = _chat(client)

    assert res.status_code == 502, res.text
    assert _pending_count() == 0


# ---------- 确认：软删，只删那一条 ----------


def test_confirm_soft_deletes_exactly_one_row(client, db):
    task = make_task(db, title="买菜")
    keep = make_task(db, title="交房租")
    action = _seed_pending(db, task.id)

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "confirmed"
    assert body["created"] == {"type": "task", "id": task.id}

    assert _is_deleted(task.id), "确认之后这条待办应当被软删"
    assert not _is_deleted(keep.id), "别的待办一条都不能动"
    # 软删不是真删：行还在，只是 deleted_at 有值
    assert _reload_task(task.id) is not None


def test_confirm_without_body_works(client, db):
    """删除卡片没有可改的字段，前端可以不带请求体。"""
    task = make_task(db, title="买菜")
    action = _seed_pending(db, task.id)

    res = client.post(f"{API}/ai/actions/{action.id}/confirm")

    assert res.status_code == 200, res.text
    assert _is_deleted(task.id)


def test_deleted_task_disappears_from_list(client, db):
    """软删之后，页面上的待办列表里就看不见了。"""
    task = make_task(db, title="买菜")
    action = _seed_pending(db, task.id)
    assert _confirm(client, action.id).status_code == 200

    listed = client.get(f"{API}/tasks").json()["items"]
    assert all(item["id"] != task.id for item in listed)


def test_prepare_payload_keeps_only_task_id_and_reason(client, db):
    """最终 payload 只认 task_id 和 reason，卡片上的 gone / 预览字段不进写库参数。"""
    task = make_task(db, title="买菜")
    action = _seed_pending(db, task.id)

    data = prepare_payload(db, action, None)

    assert data == {"task_id": task.id, "reason": "买完了"}


def test_override_cannot_change_task_id(client, db):
    """想借 override 换个 id 删另一条：extra="forbid" 直接挡下，卡片仍 pending。"""
    task = make_task(db, title="买菜")
    other = make_task(db, title="交房租")
    action = _seed_pending(db, task.id)

    res = _confirm(client, action.id, {"payload_override": {"task_id": other.id}})

    assert res.status_code == 422, res.text
    assert not _is_deleted(other.id)
    assert not _is_deleted(task.id)
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"


@pytest.mark.parametrize(
    "override",
    [
        {"title": "换个名字"},
        {"category_id": 15},
        {"status": "done"},
        {"user_id": 2},
    ],
)
def test_override_rejects_unknown_fields(client, db, override):
    """override 里塞白名单外的字段：422，什么都不会发生。"""
    task = make_task(db, title="买菜")
    action = _seed_pending(db, task.id)

    res = _confirm(client, action.id, {"payload_override": override})

    assert res.status_code == 422, res.text
    assert not _is_deleted(task.id)


def test_confirm_writes_nothing_on_failure(client, db, monkeypatch):
    """业务写失败时卡片必须原地不动 —— 不能出现「卡片已确认、待办还在」。"""
    from modules.lifeweb.ai import execute

    task = make_task(db, title="买菜")
    action = _seed_pending(db, task.id)

    def _boom(*args, **kwargs):
        raise RuntimeError("数据库炸了")

    monkeypatch.setitem(execute._WRITERS, "delete_task", _boom)

    with pytest.raises(RuntimeError):
        _confirm(client, action.id)

    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"
    assert not _is_deleted(task.id)


# ---------- 状态机：一次都不能多删 ----------


def test_second_confirm_is_409(client, db):
    task = make_task(db, title="买菜")
    action = _seed_pending(db, task.id)

    assert _confirm(client, action.id).status_code == 200
    second = _confirm(client, action.id)

    assert second.status_code == 409
    assert second.json()["code"] == CODE_ALREADY_DONE


def test_expired_delete_card_is_410(client, db):
    task = make_task(db, title="买菜")
    action = _seed_pending(db, task.id, minutes=-1)

    res = _confirm(client, action.id)

    assert res.status_code == 410
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "expired"
    assert not _is_deleted(task.id)


def test_cancelled_delete_card_cannot_confirm(client, db):
    task = make_task(db, title="买菜")
    action = _seed_pending(db, task.id)
    assert client.post(f"{API}/ai/actions/{action.id}/cancel", json={}).status_code == 200

    res = _confirm(client, action.id)

    assert res.status_code == 409
    assert not _is_deleted(task.id)


def test_task_deleted_from_page_before_confirm_is_404(client, db):
    """卡片躺在屏幕上的时候，用户在页面上把这条删了：确认时 404，不是静默成功。"""
    task = make_task(db, title="买菜")
    action = _seed_pending(db, task.id)
    with new_session() as s:
        s.get(Task, task.id).deleted_at = now_utc()
        s.commit()

    res = _confirm(client, action.id)

    assert res.status_code == 404, res.text
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"


# ---------- 兜底正文 ----------


def test_empty_content_says_delete_not_create(client, provider, db):
    """兜底正文不能说成「我整理了一条待办」—— 用户会以为要新建，点下去却是删。"""
    task = make_task(db, title="买菜")
    provider.queue(_reply(content=None, calls=[_call(task_id=task.id)]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    assert res.json()["reply"] == "我整理了一条删除，请你确认。"


def test_model_words_are_kept_when_given(client, provider, db):
    """模型自己说了话，就用它的话，不拿兜底句盖掉。"""
    task = make_task(db, title="买菜")
    provider.queue(
        _reply(content="好的，我整理了一条删除，你确认一下。", calls=[_call(task_id=task.id)])
    )

    res = _chat(client)

    assert res.json()["reply"] == "好的，我整理了一条删除，你确认一下。"