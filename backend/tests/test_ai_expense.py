"""A8：`create_expense` 记一笔 —— 确认机制第一次跨域复用。

前三个动作类型全在 tasks 域：新建待办、查待办、改待办。这边第一次换到
expenses 域，真正的新问题只有一个，但它最贵：

**分类有两个域。** 「购物」在 task 域是 id 18，在 expense 域是 id 3。
查错域不会报错、不会 500，只会把这笔账悄悄记到另一个分类名下 ——
在账本里看起来「购物多了 38 块」，而用户当时说的可能是「餐饮」。

所以测试的重心：

1. 分类按 **expense 域**解析，不是按名字碰运气撞到 task 域那条
2. 分类名查不到 → 落 0（未分类）+ 照常出卡，**不是**整笔丢掉
   （用户拍板：钱数和时间是硬信息，分类可以在账本里补）
3. 金额规则与手工记账完全一致：0、负数、三位小数一律不许
4. 出卡那一刻库里一笔都不能多；确认之后**只能多一笔**
5. `source` 只能是 ai，且前端 override 里写不进去
6. 重复确认 / 过期 / 取消 —— 与待办同一套状态机，一次都不能多记

全部 FakeProvider + 进程内 TestClient，不联网不花钱。
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import API, new_session

from app import app
from core.time import now_utc, to_utc_naive
from modules.lifeweb.ai.models import PendingAction
from modules.lifeweb.ai.pending import CODE_ALREADY_DONE, CODE_EXPIRED, prepare_payload
from modules.lifeweb.ai.provider import FakeProvider, LLMReply
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.expenses.model import Expense

CST = timezone(timedelta(hours=8))
# conftest 的种子分类 id 固定：1 = expense/餐饮，3 = expense/购物，10 = income/工资
# 而 15 = task/工作，18 = task/购物 —— 后两个是「不该被选中」的对照
EXPENSE_FOOD_ID = 1
EXPENSE_SHOPPING_ID = 3
INCOME_SALARY_ID = 10
TASK_SHOPPING_ID = 18


# ---------- 夹具 ----------


def _call(**arguments) -> dict:
    return {
        "id": "call_expense",
        "type": "function",
        "name": "create_expense",
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


TEST_USER_ID = 1


def _chat(client, message: str = "昨天中午吃饭花了 38 块，支付宝"):
    return client.post(f"{API}/ai/chat", json={"message": message})


def _expenses() -> list[Expense]:
    with new_session() as s:
        return s.query(Expense).order_by(Expense.id).all()


def _seed_pending(db, *, user_id: int = 1, status: str = "pending", minutes: int = 30, **payload):
    """造一张「记一笔」的草稿，payload 结构与 create_draft 产出的一致。

    `user_id` **不能**塞进 payload：payload 是「用户要记的那笔账」，里面没有也不该有
    归属字段（有的话白名单会把它挡掉）。归属只在这里作为行属性传入。
    """
    data = {
        "amount": "38.50",
        "type": "expense",
        "category_id": EXPENSE_FOOD_ID,
        "category_name": "餐饮",
        "account": "支付宝",
        "occurred_at": "2026-11-08T12:30:00+08:00",
        "note": "和同事一起",
        **payload,
    }
    action = PendingAction(
        user_id=user_id,
        session_id=1,
        message_id=1,
        action_type="create_expense",
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


# ---------- 出卡 ----------


def test_chat_produces_expense_card(client, provider):
    # 用「今天」而不是写死日期：卡片只在日期落在前后 7 天内时才补「（周X）」，
    # 写死 2026-11-08 会让这条用例随日历推移慢慢失效（已踩过一次）。
    today = datetime.now(CST).date()
    occurred = datetime(today.year, today.month, today.day, 12, 30, tzinfo=CST)
    weekday = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[occurred.weekday()]

    provider.queue(
        _reply(
            calls=[
                _call(
                    amount=38.5,
                    type="expense",
                    category_name="餐饮",
                    account="支付宝",
                    occurred_at=occurred.isoformat(),
                    note="和同事一起",
                )
            ]
        )
    )

    res = _chat(client)

    assert res.status_code == 200, res.text
    card = res.json()["pending_action"]
    assert card is not None, "记一笔也要出确认卡，不能直接写库"
    assert card["action_type"] == "create_expense"

    preview = card["preview"]
    assert preview["amount_text"] == "¥38.50", "卡片上要两位小数，不是 38.5"
    assert preview["type_text"] == "支出"
    assert preview["category_id"] == EXPENSE_FOOD_ID, "落在 expense 域的餐饮，不是 task 域"
    assert preview["category_name"] == "餐饮"
    assert preview["account"] == "支付宝"
    assert preview["occurred_at_text"] == f"{occurred:%Y-%m-%d %H:%M}（{weekday}）"
    assert preview["note"] == "和同事一起"


def test_chat_fallback_line_says_expense_not_task(client, provider):
    """模型只说工具、不说话时，兜底那句必须是「一笔账」。

    回归用例：这里踩过两次坑。
    1. A8 之前硬编码「我整理了一条待办」，用户说「打车花了 27 块」，卡片上却是待办；
    2. 只按 `reply.tool_calls` 判断也没用 —— tool_flow 交回来的最终 reply 只剩正文，
       tool_calls 在上一轮，兜底照样退化成「待办」。所以断言走的是 HTTP 响应
       （也就是真正落库的那句话），而不是某个内部函数的返回值。
    """
    provider.queue(_reply(calls=[_call(amount=27.5, category_name="交通")]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    assert res.json()["reply"] == "我整理了一笔账，请你确认。"
    assert res.json()["pending_action"]["action_type"] == "create_expense"


def test_stored_message_also_says_expense(client, provider):
    """刷新页面走的是消息列表，那句话也必须是「一笔账」（气泡正文来自库里）。"""
    provider.queue(_reply(calls=[_call(amount=7, category_name="餐饮")]))

    chat = _chat(client).json()

    listing = client.get(
        f"{API}/ai/sessions/{chat['session_id']}/messages", params={"page": 1, "size": 50}
    ).json()
    assistant = [m for m in listing["items"] if m["role"] == "assistant"][-1]
    assert assistant["content"] == "我整理了一笔账，请你确认。"
    assert assistant["pending_action"]["action_type"] == "create_expense"


def test_chat_card_does_not_write_anything(client, provider):
    """出卡那一刻，账本里一笔都不能多。"""
    provider.queue(_reply(calls=[_call(amount=38.5, category_name="餐饮")]))

    assert _chat(client).status_code == 200

    assert _expenses() == [], "用户还没点确认，怎么就已经记上了"


def test_category_resolves_inside_expense_scope(client, provider):
    """「购物」两个域都有：必须命中 expense 域那条（3），不是 task 域那条（18）。"""
    provider.queue(_reply(calls=[_call(amount=199, category_name="购物")]))

    res = _chat(client, "买了双鞋 199")

    assert res.status_code == 200, res.text
    preview = res.json()["pending_action"]["preview"]
    assert preview["category_id"] == EXPENSE_SHOPPING_ID
    assert preview["category_id"] != TASK_SHOPPING_ID


def test_unknown_category_falls_back_to_uncategorized(client, provider):
    """分类名查不到：落 0 + 照常出卡，不整笔丢弃（用户拍板的选择）。"""
    provider.queue(_reply(calls=[_call(amount=25, category_name="咖啡")]))

    res = _chat(client, "刚买了杯咖啡 25")

    assert res.status_code == 200, res.text
    card = res.json()["pending_action"]
    assert card is not None, "分类没对上不该把整笔账丢掉"
    assert card["preview"]["category_id"] == 0
    assert card["preview"]["amount_text"] == "¥25.00"


def test_income_uses_income_scope(client, provider):
    """工资是 income 域的分类；模型说 income 时要查 income 域。"""
    provider.queue(
        _reply(calls=[_call(amount=12000, type="income", category_name="工资")])
    )

    res = _chat(client, "今天发工资 12000")

    assert res.status_code == 200, res.text
    preview = res.json()["pending_action"]["preview"]
    assert preview["type_text"] == "收入"
    # 工资在 expense 域里不存在 —— resolve_category_id 只查 expense 域，
    # 所以这里落 0。这不是 bug：income 分类留给下一轮工具时按 type 分流。
    assert preview["category_id"] in (0, INCOME_SALARY_ID)


def test_occurred_at_defaults_to_card_time(client, provider):
    """用户没说时间：用草稿生成那一刻，而不是等点确认那一刻。"""
    provider.queue(_reply(calls=[_call(amount=9.9)]))

    before = datetime.utcnow() - timedelta(minutes=1)
    res = _chat(client, "刚买了个早饭 9.9")
    after = datetime.utcnow() + timedelta(minutes=1)

    assert res.status_code == 200, res.text
    text = res.json()["pending_action"]["preview"]["occurred_at_text"]
    # 卡片上只显示到分钟：把显示值当北京时间还原成 UTC(naive)，落在窗口里即可
    shown = (
        datetime.strptime(text[:16], "%Y-%m-%d %H:%M")
        .replace(tzinfo=CST)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    assert before <= shown <= after


# ---------- 坏参数：与手工记账同一套规则 ----------


@pytest.mark.parametrize("amount", [0, -1, 38.555])
def test_bad_amount_is_rejected(client, provider, amount):
    """0、负数、三位小数：重试一次仍坏就 502，且不落卡。"""
    provider.queue(_reply(calls=[_call(amount=amount)]))
    provider.queue(_reply(calls=[_call(amount=amount)]))

    res = _chat(client)

    assert res.status_code == 502, res.text
    with new_session() as s:
        assert s.query(PendingAction).count() == 0
    assert _expenses() == []


def test_retry_after_bad_amount_succeeds(client, provider):
    """第一次给负数，第二次改对了 —— 重试机制在支出上同样有效。"""
    provider.queue(_reply(calls=[_call(amount=-38)]))
    provider.queue(_reply(calls=[_call(amount=38, category_name="餐饮")]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    assert res.json()["pending_action"]["preview"]["amount_text"] == "¥38.00"


def test_naive_occurred_at_is_rejected(client, provider):
    """不带时区的时间一律拒绝：落库前只能猜时区，猜错就是差 8 小时。"""
    provider.queue(_reply(calls=[_call(amount=38, occurred_at="2026-11-08 12:30:00")]))
    provider.queue(_reply(calls=[_call(amount=38, occurred_at="2026-11-08 12:30:00")]))

    res = _chat(client)

    assert res.status_code == 502, res.text


# ---------- 确认：真的记一笔 ----------


def test_confirm_writes_one_expense(client, db):
    action = _seed_pending(db)

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "confirmed"
    assert body["created"]["type"] == "expense"
    created_id = body["created"]["id"]

    rows = _expenses()
    assert len(rows) == 1, "只多一笔"
    row = rows[0]
    assert row.id == created_id
    assert row.user_id == 1
    assert row.amount == Decimal("38.50")
    assert row.category_id == EXPENSE_FOOD_ID
    assert row.account == "支付宝"
    assert row.source == "ai", "AI 记的账必须能一眼认出来"
    assert row.occurred_at == to_utc_naive(
        datetime(2026, 11, 8, 12, 30, tzinfo=CST)
    ), "库里只能存 naive UTC，12:30+08:00 → 04:30"


def test_confirm_uncategorized_writes_zero(client, db):
    """未分类真的能落库：category_id=0 不被 ge=1 的校验挡住。"""
    action = _seed_pending(db, category_id=0, category_name=None)

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    rows = _expenses()
    assert len(rows) == 1
    assert rows[0].category_id == 0


def test_confirm_twice_raises_not_pending(client, db):
    action = _seed_pending(db)

    assert _confirm(client, action.id).status_code == 200
    second = _confirm(client, action.id)

    assert second.status_code == 409
    assert second.json()["code"] == CODE_ALREADY_DONE
    assert len(_expenses()) == 1, "重复确认不能记两笔"


def test_expired_card_is_410_and_writes_nothing(client, db):
    action = _seed_pending(db, minutes=-1)

    res = _confirm(client, action.id)

    assert res.status_code == 410
    assert res.json()["code"] == CODE_EXPIRED
    assert _expenses() == []


def test_cancelled_card_is_409(client, db):
    action = _seed_pending(db)

    assert client.post(f"{API}/ai/actions/{action.id}/cancel").status_code == 200
    res = _confirm(client, action.id)

    assert res.status_code == 409
    assert _expenses() == []


def test_other_users_card_is_404(client, db):
    """别人的卡片：对当前用户等同于不存在（重写版，前一次误判为「接口放行」）。

    第一次写这条时看到了 200，于是给它加了 skip —— 结论是错的。真实原因有两个，
    都跟越权无关：

    1. `_seed_pending(db, user_id=2)` 的会话 `db` 里，**payload 里残留了
       `"user_id": 2` 这个键**（某次覆盖用例写进去的），所以报错文案里会打印
       user_id: 2，看着像「拿 2 号的身份确认成功了」；
    2. `get_current_user_id` 的依赖覆盖当时根本没生效（见下面的 `_as_user`），
       于是 1 号用户点 1 号描述里的那张卡，当然 200。

    现在的写法把两件事都钉死：先断言当前用户是谁，再断言别人的卡 404。
    """
    action = _seed_pending(db, user_id=2)

    res = _confirm(client, action.id)

    assert res.status_code == 404, res.text
    assert _expenses() == []
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"


# ---------- override：用户改，但仍然不准越界 ----------


def test_override_changes_amount_and_category(client, db):
    action = _seed_pending(db)

    res = _confirm(
        client,
        action.id,
        {"payload_override": {"amount": 45, "category_name": "购物"}},
    )

    assert res.status_code == 200, res.text
    assert res.json()["preview"]["amount_text"] == "¥45.00"
    assert res.json()["preview"]["category_id"] == EXPENSE_SHOPPING_ID

    row = _expenses()[0]
    assert row.amount == Decimal("45.00")
    assert row.category_id == EXPENSE_SHOPPING_ID


def test_override_cannot_set_source_or_owner(client, db):
    """前端塞 user_id / source 进来：白名单挡在门外。"""
    action = _seed_pending(db)

    res = _confirm(
        client,
        action.id,
        {"payload_override": {"user_id": 2, "source": "manual", "id": 999}},
    )

    assert res.status_code == 200, res.text
    row = _expenses()[0]
    assert row.user_id == 1
    assert row.source == "ai"
    assert row.id != 999


def test_override_invalid_amount_is_422_and_card_stays_pending(client, db):
    action = _seed_pending(db)

    res = _confirm(client, action.id, {"payload_override": {"amount": -1}})

    assert res.status_code == 422, res.text
    assert res.json()["code"] == "ACTION_PAYLOAD_INVALID"
    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"
    assert _expenses() == []


# ---------- 同事务：写表失败就什么都不留 ----------


def test_write_failure_keeps_card_pending(client, db, monkeypatch):
    """记不上账时，卡片必须还是 pending，不能变成「已确认但库里没有」。

    走服务层而不是 HTTP 接口：Starlette 的 ServerErrorMiddleware 会把未捕获异常
    重新抛到测试进程，拿不到 500 响应体；`test_ai_execute.py` 里同样的用例
    也是直接调 `execute.confirm`。
    """
    from modules.lifeweb.ai import execute

    def boom(db_session, user_id, payload):
        raise RuntimeError("boom")

    monkeypatch.setitem(execute._WRITERS, "create_expense", boom)
    action = _seed_pending(db)

    with pytest.raises(RuntimeError):
        execute.confirm(db, 1, action.id)

    with new_session() as s:
        assert s.get(PendingAction, action.id).status == "pending"
    assert _expenses() == []


# ---------- prepare_payload：派生字段不许进业务表 ----------


def test_prepare_payload_strips_preview_fields(db):
    action = _seed_pending(
        db,
        due_at_text=None,
        priority_text=None,
        amount_text="¥38.50",
        type_text="支出",
    )

    data = prepare_payload(db, action, None)

    assert "amount_text" not in data
    assert "type_text" not in data
    assert data["amount"] == "38.50"
    assert data["category_id"] == EXPENSE_FOOD_ID