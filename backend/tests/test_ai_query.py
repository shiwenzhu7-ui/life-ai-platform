"""A6：`query_tasks` 只读查询 —— 让助手查得到自己的待办。

`test_ai_tool_flow.py` 证明的是「写入不越界」；这边证明「**读也不越界**」。
全部用 FakeProvider，不联网不花钱。要证明的八件事：

1. 查询真的落到数据库上（不是模型编的），且只查自己名下的
2. 软删的、别人的、分类对不上的，一条都不能回灌给模型
3. `limit` 夹在 1..10，模型传 1000 也炸不了请求体
4. 参数坏掉时降级为无筛选照常查，而不是让整条消息失败
5. 查询不建卡、不写业务表 —— 一轮只读对话结束后 `tasks` 一行都不多
6. 轮次上限是 3，模型无限想查也停得下来，且最后一轮一定有句人话
7. 「查完再建卡」这一整条链能通（这是 A6 的存在意义）
8. 多轮调用的 token 要累加进用量日志，不能只记最后一轮
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import API, make_task, new_session

from app import app
from modules.lifeweb.ai.models import PendingAction, UsageLog
from modules.lifeweb.ai.provider import FakeProvider, LLMReply, merge_replies
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.ai.tool_flow import MAX_ROUNDS, run_tool_flow
from modules.lifeweb.tasks.model import Task

CST = timezone(timedelta(hours=8))


def _call(name: str = "query_tasks", **arguments) -> dict:
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


def _chat(client, message: str = "我还有哪些没做", session_id: int | None = None):
    body = {"message": message}
    if session_id is not None:
        body["session_id"] = session_id
    return client.post(f"{API}/ai/chat", json=body)


def _tool_contents(provider: FakeProvider, call_index: int = 1) -> str:
    """第 `call_index` 次模型调用收到的最后一条消息 —— 就是回灌的工具结果。"""
    messages = provider.calls[call_index]["messages"]
    return messages[-1]["content"]


def _task_count() -> int:
    with new_session() as s:
        return s.query(Task).count()


# ---------- 正常查询 ----------


def test_query_returns_real_rows(client, provider, db):
    """模型问「我还有啥没做」，回灌的内容里必须出现库里那几条的标题。"""
    make_task(db, title="买牛奶")
    make_task(db, title="交房租", status="doing")
    make_task(db, title="已经做完的事", status="done")
    provider.queue(_reply(calls=[_call()])).queue(_reply("你有两件事没做完。"))

    res = _chat(client, "我还有哪些没做")

    assert res.status_code == 200, res.text
    fed = _tool_contents(provider)
    assert "买牛奶" in fed
    # doing 也算没做完：只查 todo 会让用户以为自己在做的事凭空消失了
    assert "交房租" in fed
    assert "已经做完的事" not in fed
    assert res.json()["reply"] == "你有两件事没做完。"


def test_query_keeps_done_when_only_done_requested(client, provider, db):
    """反过来也要对：用户问「哪些做完了」，done 必须查得到。"""
    make_task(db, title="已经做完的事", status="done")
    make_task(db, title="还没做的")
    provider.queue(_reply(calls=[_call(status="done")])).queue(_reply("做完一件。"))

    _chat(client, "我都完成了哪些")

    fed = _tool_contents(provider)
    assert "已经做完的事" in fed
    assert "还没做的" not in fed


def test_query_feeds_back_a_list_not_json(client, provider, db):
    """回灌的是人话清单，不是 model_dump 出来的 JSON。

    直接把 ORM 行 dump 成 JSON 会带 user_id / version / created_at，
    白占 token，而且模型开始用字段名回答用户（「你的 status 是 todo」）。
    """
    make_task(db, title="买牛奶", due_at=datetime(2026, 11, 9, 7, 0))  # UTC
    provider.queue(_reply(calls=[_call()])).queue(_reply("好的"))

    _chat(client)

    fed = _tool_contents(provider)
    assert "title=买牛奶" in fed
    # 库里存的是 naive UTC 07:00，回灌必须是北京时间 15:00，否则差 8 小时
    assert "2026-11-09 15:00" in fed
    for noisy in ("user_id", "version", "created_at"):
        assert noisy not in fed


def test_query_only_sees_own_tasks(client, provider, db):
    """别人的待办一个字都不能出现在回灌内容里。"""
    make_task(db, user_id=2, title="别人的私事")
    make_task(db, user_id=1, title="我的事")
    provider.queue(_reply(calls=[_call()])).queue(_reply("只有一件事。"))

    _chat(client)

    fed = _tool_contents(provider)
    assert "我的事" in fed
    assert "别人的私事" not in fed


def test_query_skips_deleted(client, provider, db):
    """删掉的待办不能复活在 AI 的嘴里。"""
    gone = make_task(db, title="删掉的垃圾")
    client.delete(f"{API}/tasks/{gone.id}")
    make_task(db, title="还活着")
    provider.queue(_reply(calls=[_call()])).queue(_reply("嗯"))

    _chat(client)

    fed = _tool_contents(provider)
    assert "还活着" in fed
    assert "删掉的垃圾" not in fed


def test_query_filters_by_keyword(client, provider, db):
    make_task(db, title="买牛奶")
    make_task(db, title="写周报")
    provider.queue(_reply(calls=[_call(keyword="牛奶")])).queue(_reply("就是买牛奶那条。"))

    _chat(client, "买牛奶那条是什么时候")

    fed = _tool_contents(provider)
    assert "买牛奶" in fed
    assert "写周报" not in fed


def test_query_keyword_wildcards_are_escaped(client, provider, db):
    """用户（或模型）传 `%` 不能变成「匹配全部」。"""
    make_task(db, title="折扣 100% 的券")
    make_task(db, title="完全无关的事")
    provider.queue(_reply(calls=[_call(keyword="100%")])).queue(_reply("找到了。"))

    _chat(client, "那张 100% 的券呢")

    fed = _tool_contents(provider)
    assert "折扣 100% 的券" in fed
    assert "完全无关的事" not in fed


def test_query_filters_by_category(client, provider, db):
    """「购物」在 task 域是 18；按分类查不能把别的分类捞进来。"""
    make_task(db, title="买洗衣液", category_id=18)
    make_task(db, title="改简历", category_id=15)
    provider.queue(_reply(calls=[_call(category_name="购物")])).queue(_reply("购物类只有这一条。"))

    _chat(client, "购物类有什么")

    fed = _tool_contents(provider)
    assert "买洗衣液" in fed
    assert "改简历" not in fed


def test_query_unknown_category_returns_empty(client, provider, db):
    """模型编了个不存在的分类：给空结果并写清原因，不能退化成「查全部」。"""
    make_task(db, title="买牛奶")
    provider.queue(_reply(calls=[_call(category_name="修仙")])).queue(_reply("没有这个分类。"))

    _chat(client, "修仙类有什么")

    fed = _tool_contents(provider)
    assert "修仙" in fed
    assert "买牛奶" not in fed


def test_query_due_before_uses_beijing_time(client, provider, db):
    """「这周有什么」→ 换算成 due_before；晚于它的一条都不能出现。"""
    make_task(db, title="周内的事", due_at=datetime(2026, 11, 12, 2, 0))  # 北京 10:00
    make_task(db, title="下周的事", due_at=datetime(2026, 11, 20, 2, 0))
    before = "2026-11-15T00:00:00+08:00"
    provider.queue(_reply(calls=[_call(due_before=before)])).queue(_reply("这周一件。"))

    _chat(client, "这周有什么安排")

    fed = _tool_contents(provider)
    assert "周内的事" in fed
    assert "下周的事" not in fed


def test_query_limit_is_clamped(client, provider, db):
    """模型传 limit=1000 也只回 10 条 —— 这条链路的成本闸门在这里。"""
    for i in range(15):
        make_task(db, title=f"待办{i:02d}")
    provider.queue(_reply(calls=[_call(limit=1000)])).queue(_reply("很多。"))

    _chat(client)

    fed = _tool_contents(provider)
    # 15 条里最多只该出现 10 个「- title=」——limit 在 payload 校验和 crud.search 里各夹一次
    assert fed.count("- title=") == 10


def test_query_empty_result_says_so(client, provider, db):
    """没查到就直接说没有，而不是沉默。"""
    provider.queue(_reply(calls=[_call()])).queue(_reply("目前没有待办。"))

    res = _chat(client)

    assert "没有符合条件的记录" in _tool_contents(provider)
    assert res.json()["reply"] == "目前没有待办。"


# ---------- 安全性：只读不写 ----------


def test_query_never_writes_business_data(client, db):
    """只读对话结束后，tasks 表一行都不多、也不产生待确认卡片。"""
    make_task(db, title="买牛奶")
    fake = FakeProvider()
    fake.queue(_reply(calls=[_call()])).queue(_reply("只有买牛奶这一条。"))
    app.dependency_overrides[get_provider] = lambda: fake
    try:
        res = _chat(client)
    finally:
        app.dependency_overrides.pop(get_provider, None)

    assert res.status_code == 200, res.text
    assert res.json()["pending_action"] is None
    assert _task_count() == 1
    with new_session() as s:
        assert s.query(PendingAction).count() == 0


def test_bad_arguments_degrade_to_unfiltered(client, provider, db):
    """参数是坏 JSON 时按无筛选查，用户照样看到清单，而不是收到 5xx。"""
    make_task(db, title="买牛奶")
    broken = {"id": "c1", "type": "function", "name": "query_tasks", "arguments": "{oops"}
    provider.queue(_reply(calls=[broken])).queue(_reply("我看到一条。"))

    res = _chat(client)

    assert res.status_code == 200, res.text
    fed = _tool_contents(provider)
    assert "买牛奶" in fed
    assert "没通过校验" in fed  # 让模型知道自己这次的条件没生效


def test_unknown_status_is_treated_as_no_filter(client, provider, db):
    """模型传了个不在枚举里的 status：整条参数作废，但仍按无筛选查。"""
    make_task(db, title="买牛奶")
    provider.queue(_reply(calls=[_call(status="whatever")])).queue(_reply("一条。"))

    res = _chat(client)

    assert res.status_code == 200, res.text
    assert "买牛奶" in _tool_contents(provider)


# ---------- 轮次控制 ----------


def test_loop_stops_at_max_rounds(client, provider, db):
    """模型无限想查：最多调 MAX_ROUNDS 次模型，且最后一次之后一定有正文。"""
    make_task(db, title="买牛奶")
    for _ in range(MAX_ROUNDS + 2):
        provider.queue(_reply(calls=[_call()]))
    provider.queue(_reply("查完了。"))

    res = _chat(client)

    assert res.status_code == 200, res.text
    assert len(provider.calls) == MAX_ROUNDS
    body = res.json()
    assert body["reply"]  # 绝不能是空串
    assert body["pending_action"] is None


def test_loop_empty_final_content_falls_back_to_the_rows(client, provider, db):
    """轮数用尽时模型一个字都没说 —— 把清单当正文，而不是一条空气泡。"""
    make_task(db, title="买牛奶")
    for _ in range(MAX_ROUNDS):
        provider.queue(_reply(calls=[_call()]))  # 每轮都只调工具，正文为空

    res = _chat(client)

    assert res.status_code == 200, res.text
    reply = res.json()["reply"]
    assert "买牛奶" in reply


def test_tokens_are_summed_across_rounds(client, provider, db):
    """多轮调用的 token 必须累加 —— 只记最后一轮会让月底的账对不上。"""
    provider.queue(_reply(calls=[_call()], tokens=(100, 20)))
    provider.queue(_reply("两件事。", tokens=(50, 10)))

    _chat(client)

    with new_session() as s:
        log = s.query(UsageLog).order_by(UsageLog.id.desc()).first()
        assert log.token_in == 150
        assert log.token_out == 30


# ---------- 查完再写：A6 的真实用途 ----------


def test_query_then_create_task_chain(client, provider, db):
    """先查、再按查到的结果建卡：这是 A6 存在的意义。"""
    make_task(db, title="交房租")
    provider.queue(_reply(calls=[_call()]))
    provider.queue(
        _reply(
            calls=[
                _call(
                    "create_task",
                    title="交房租（补交）",
                    priority="high",
                )
            ]
        )
    )

    res = _chat(client, "帮我把交房租这事再记一遍")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["pending_action"]["action_type"] == "create_task"
    assert body["pending_action"]["preview"]["title"] == "交房租（补交）"
    # 第二轮模型收到的上下文里必须有第一轮查到的结果
    assert "交房租" in provider.calls[1]["messages"][-1]["content"]
    # 仍然没有写业务表：卡片只是草稿
    assert _task_count() == 1


def test_write_wins_and_skips_reads(client, provider, db):
    """同一轮里既申请 query 又申请 create_task：按写入走，不留半截查询记录。"""
    provider.queue(
        _reply(
            calls=[
                _call("query_tasks"),
                _call("create_task", title="买牛奶"),
            ]
        )
    )

    res = _chat(client, "记一下买牛奶")

    assert res.status_code == 200, res.text
    assert res.json()["pending_action"]["preview"]["title"] == "买牛奶"
    assert len(provider.calls) == 1  # 写入出现即收手，不再多问一次


# ---------- 单元层 ----------


def test_run_tool_flow_reports_rounds_and_reads(db):
    """直接调 run_tool_flow：轮次与查询记录要能看见（测试与日志都依赖它）。"""
    class _P:
        def __init__(self):
            self.calls = []

        def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=800):
            self.calls.append({"messages": messages, "tools": tools})
            if len(self.calls) == 1:
                return _reply(calls=[_call()])
            return _reply("查完了。")

    session = _make_session(db)
    flow = run_tool_flow(
        db,
        _P(),
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "我还有啥"}],
        user_id=1,
        session=session,
        message_id=_make_message(db, session).id,
    )

    assert flow.ok is True
    assert flow.rounds == 2
    assert flow.reads and flow.reads[0]["tool"] == "query_tasks"
    assert flow.pending is None
    assert flow.reply.content == "查完了。"


def test_merge_replies_sums_tokens_and_keeps_last_content():
    a = _reply("第一轮", tokens=(10, 1))
    b = _reply("第二轮", tokens=(20, 2), latency_ms=300)
    merged = merge_replies([a, b])

    assert merged.content == "第二轮"
    assert merged.token_in == 30
    assert merged.token_out == 3
    assert merged.latency_ms == 300


def test_merge_replies_rejects_empty_list():
    """空列表是调用方的 bug，不能静默合成一个空回复。"""
    with pytest.raises(ValueError):
        merge_replies([])


# ---------- 辅助 ----------


def _make_session(db):
    from modules.lifeweb.ai.models import ChatSession

    session = ChatSession(user_id=1, title="测试会话", model="deepseek-chat")
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_message(db, session):
    from modules.lifeweb.ai.models import ChatMessage

    message = ChatMessage(
        user_id=1, session_id=session.id, role="assistant", content=None, status="ok"
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message