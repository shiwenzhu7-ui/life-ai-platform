"""V1.4：`add_shopping_item` —— 第 10 个工具，也是唯一一个「一次写多行」的写入工具。

前六个写工具都是「一张卡 → 一行数据」。购物可以一口气加三四件，于是多了
一组别的地方没有的风险：

1. **加了一半**。第三件炸了前两件已经落库 —— 用户看到的是错误提示，
   清单里却多出两条。所以写入必须在同一个事务里，要么全加，要么一条不加。
2. **凭空多出一张清单**。用户说「加进周末采购」，而这张清单不存在时，
   确认会顺手建一张。它必须在**卡片上就写明**，不能等点完才发现。
3. **清单跑了**。卡片出好、用户还没点，这期间清单被删了 —— 不能 500，
   也不能什么都不加还报成功。

测试重心就是这三条，外加「出卡那一刻库里一条都不能多」这条 A5 起的老规矩。

全部 FakeProvider + 进程内 TestClient，不联网不花钱。
"""

import json
import uuid
from datetime import timedelta

import pytest

from conftest import API, new_session

from app import app
from core.time import now_utc
from modules.lifeweb.ai.models import PendingAction
from modules.lifeweb.ai.provider import FakeProvider, LLMReply
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.shopping.model import ShoppingItem, ShoppingList

DEFAULT_LIST = "默认清单"


# ---------- 夹具 ----------


def _call(**arguments) -> dict:
    return {
        "id": "call_shopping",
        "type": "function",
        "name": "add_shopping_item",
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


def _chat(client, message: str = "帮我记一下要买牛奶和鸡蛋"):
    return client.post(f"{API}/ai/chat", json={"message": message})


def _lists() -> list[ShoppingList]:
    with new_session() as s:
        return s.query(ShoppingList).order_by(ShoppingList.id).all()


def _items() -> list[ShoppingItem]:
    with new_session() as s:
        return s.query(ShoppingItem).order_by(ShoppingItem.id).all()


def _seed_list(name: str, *, user_id: int = 1, status: str = "active") -> int:
    """直接插一张清单 —— 购物清单没有「用户」外键约束，建对象再 commit 就够。"""
    with new_session() as s:
        row = ShoppingList(user_id=user_id, name=name, status=status)
        s.add(row)
        s.commit()
        return row.id


def _seed_pending(db, *, user_id: int = 1, status: str = "pending", minutes: int = 30, **overrides):
    """造一张「加入清单」的草稿，payload 与 create_draft 的产出同形。

    默认指着**不存在**的清单，`list_exists=False` —— 这是最容易出错的那种卡。
    """
    data = {
        "list_name": "周末采购",
        "list_exists": False,
        "list_id": None,
        "items": [{"name": "牛奶", "quantity": "2", "unit": "盒"}],
        **overrides,
    }
    action = PendingAction(
        user_id=user_id,
        session_id=1,
        message_id=1,
        action_type="add_shopping_item",
        payload=data,
        status=status,
        idempotency_key=str(uuid.uuid4()),
        expires_at=now_utc() + timedelta(minutes=minutes),
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def _confirm(client, action_id: int, override: dict | None = None):
    """点确认。

    `override` 是**用户改过的那部分草稿**，接口要求的形状是
    `{"payload_override": {...}}` —— 直接传裸 dict 会被 `ActionConfirmRequest`
    静默丢掉（它没有 items 字段），测试就会假装「改了但没生效其实是没传」。
    """
    body = {"payload_override": override} if override is not None else {}
    return client.post(f"{API}/ai/actions/{action_id}/confirm", json=body)


# ---------- 出卡 ----------


def test_chat_produces_shopping_card(client, provider):
    provider.queue(
        _reply(calls=[_call(list_name="周末采购", items=[{"name": "牛奶", "quantity": 2, "unit": "盒"}])])
    )

    res = _chat(client)

    assert res.status_code == 200, res.text
    card = res.json()["pending_action"]
    assert card is not None, "加购物也要出确认卡，不能直接写库"
    assert card["action_type"] == "add_shopping_item"

    preview = card["preview"]
    assert preview["list_name_text"] == "周末采购"
    assert preview["list_exists"] is False, "清单还不存在，卡上必须写明会新建"
    assert preview["items_text"] == [{"name": "牛奶", "quantity_text": "2", "unit": "盒"}]

    # 出卡那一刻库里不能有任何东西
    assert _lists() == []
    assert _items() == []


def test_chat_without_list_name_uses_default_list(client, provider):
    provider.queue(_reply(calls=[_call(items=[{"name": "酱油"}])]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    preview = res.json()["pending_action"]["preview"]
    assert preview["list_name_text"] == DEFAULT_LIST
    assert preview["list_exists"] is False
    assert preview["items_text"][0]["quantity_text"] == "1", "没说数量默认 1"
    assert preview["items_text"][0]["unit"] == "个", "没说单位默认「个」"


def test_existing_list_is_marked_as_existing(client, provider):
    _seed_list("周末采购")
    provider.queue(_reply(calls=[_call(list_name="周末采购", items=[{"name": "牛奶"}])]))

    res = _chat(client)

    preview = res.json()["pending_action"]["preview"]
    assert preview["list_exists"] is True, "清单已存在时不能吓唬用户「将新建」"
    assert preview["list_id"] is not None


def test_chat_fallback_line_says_shopping_not_task(client, provider):
    """模型只调工具不说话时，兜底句不能是「我整理了一条待办」。"""
    provider.queue(_reply(calls=[_call(items=[{"name": "牛奶"}])]))

    res = _chat(client)

    assert res.status_code == 200, res.text
    assert res.json()["reply"] == "我整理了一份购物清单，请你确认。"


def test_blank_quantity_and_unit_fall_back_to_defaults(client, provider):
    provider.queue(_reply(calls=[_call(items=[{"name": "鸡蛋", "quantity": None, "unit": "  "}])]))

    res = _chat(client)

    preview = res.json()["pending_action"]["preview"]
    assert preview["items_text"] == [{"name": "鸡蛋", "quantity_text": "1", "unit": "个"}]


def test_too_many_items_is_rejected(client, provider):
    """超过 10 件：参数校验失败 → 这一轮直接失败，不出卡、不落库。

    一张列了二十行的卡没人会核对。校验失败的 tool_call 会被丢掉、回灌错误原文
    重试一轮；重试轮没有可用草稿就按 `MALFORMED_TOOL_ARGS` 失败（见
    `tool_flow.py` 开头第 3 条：不能把「好的，已经帮你记下了」+ 没卡片当成成功）。
    """
    provider.queue(
        _reply(calls=[_call(items=[{"name": f"东西{i}"} for i in range(11)])])
    )
    provider.queue(_reply(content="东西有点多，一次最多加 10 样"))

    res = _chat(client)

    assert res.status_code == 502, res.text
    assert res.json()["code"] == "MALFORMED_TOOL_ARGS"
    assert _lists() == []
    assert _items() == []


def test_quantity_must_be_positive(client, provider):
    provider.queue(_reply(calls=[_call(items=[{"name": "牛奶", "quantity": 0}])]))
    provider.queue(_reply(content="数量得是正数"))

    res = _chat(client)

    assert res.status_code == 502, res.text
    assert res.json()["code"] == "MALFORMED_TOOL_ARGS"
    assert _items() == []


# ---------- 确认 ----------


def test_confirm_creates_default_list_and_items(client, db):
    action = _seed_pending(db, list_name=None)

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"]["type"] == "shopping_list"

    lists = _lists()
    assert len(lists) == 1
    assert lists[0].name == DEFAULT_LIST

    items = _items()
    assert len(items) == 1
    assert items[0].name == "牛奶"
    assert str(items[0].quantity) == "2.00"
    assert items[0].unit == "盒"
    assert items[0].source == "ai", "助手写的必须是 ai，这是来源证据"


def test_confirm_uses_existing_list(client, db):
    list_id = _seed_list("周末采购")
    action = _seed_pending(db, list_name="周末采购", list_exists=True, list_id=list_id)

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    lists = _lists()
    assert len(lists) == 1, "清单已存在就不能再建一张"
    assert lists[0].id == list_id
    assert len(_items()) == 1


def test_confirm_adds_multiple_items_in_one_go(client, db):
    action = _seed_pending(
        db,
        items=[
            {"name": "牛奶", "quantity": "2", "unit": "盒"},
            {"name": "鸡蛋", "quantity": "10", "unit": "个"},
            {"name": "面包", "quantity": "1", "unit": "个"},
        ],
    )

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    items = _items()
    assert [i.name for i in items] == ["牛奶", "鸡蛋", "面包"]
    assert all(i.source == "ai" for i in items)
    # 同一清单、sort_order 递增（没写拖动排序，但顺序要稳定）
    assert len({i.list_id for i in items}) == 1
    assert [i.sort_order for i in items] == sorted(i.sort_order for i in items)


def test_item_source_cannot_be_faked(client, db):
    """payload_override 里塞 source=manual 也没用：白名单里没有这个字段。"""
    action = _seed_pending(db)

    res = _confirm(
        client,
        action.id,
        {"items": [{"name": "牛奶", "quantity": "2", "unit": "盒", "source": "manual"}]},
    )

    assert res.status_code == 422, res.text
    assert _items() == [], "校验没过就不能有任何一行落库"


def test_override_can_change_items(client, db):
    action = _seed_pending(db)

    res = _confirm(client, action.id, {"items": [{"name": "酸奶", "quantity": "3", "unit": "杯"}]})

    assert res.status_code == 200, res.text
    items = _items()
    assert [i.name for i in items] == ["酸奶"]


def test_empty_items_is_rejected(client, db):
    action = _seed_pending(db)

    res = _confirm(client, action.id, {"items": []})

    assert res.status_code == 422, res.text


# ---------- 状态机 ----------


def test_confirm_twice_adds_only_once(client, db):
    action = _seed_pending(db)

    first = _confirm(client, action.id)
    second = _confirm(client, action.id)

    assert first.status_code == 200
    assert second.status_code == 409, "同一张卡不能确认两次"
    assert len(_items()) == 1, "第二次不能重复加"


def test_cancel_writes_nothing(client, db):
    action = _seed_pending(db)

    res = client.post(f"{API}/ai/actions/{action.id}/cancel")

    assert res.status_code == 200, res.text
    assert _lists() == []
    assert _items() == []


def test_expired_card_is_410(client, db):
    action = _seed_pending(db, minutes=-1)

    res = _confirm(client, action.id)

    assert res.status_code == 410, res.text
    assert _items() == []


def test_other_users_card_is_404(client, db):
    action = _seed_pending(db, user_id=2)

    res = _confirm(client, action.id)

    assert res.status_code == 404, res.text
    assert _items() == []


# ---------- 列表在出卡之后跑了 ----------


def test_list_deleted_after_card_is_recreated(client, db):
    """卡片上写「加进已有的周末采购」，点之前这张清单被删了。

    不能 500，也不能假装加进去 —— 按最新的库状态建一张新的，数据不丢。
    """
    list_id = _seed_list("周末采购")
    action = _seed_pending(db, list_name="周末采购", list_exists=True, list_id=list_id)

    with new_session() as s:
        row = s.get(ShoppingList, list_id)
        s.delete(row)
        s.commit()

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    lists = _lists()
    assert len(lists) == 1
    assert lists[0].name == "周末采购", "原来的名字还在，不该变成「周末采购(1)」"
    assert len(_items()) == 1


def test_archived_list_is_reused(client, db):
    """同名清单归档后，名字会被腾出来改成一号副本，所以原名一定对应活跃清单；
    但如果用户点名的那张恰好已被归档改名，就按名字找不到 → 新建一张同名的。
    这里验证的是「不会因为存在归档同名行而报唯一键冲突」。"""
    _seed_list("周末采购(1)", status="archived")
    action = _seed_pending(db, list_name="周末采购")

    res = _confirm(client, action.id)

    assert res.status_code == 200, res.text
    assert len(_lists()) == 2
    assert len(_items()) == 1