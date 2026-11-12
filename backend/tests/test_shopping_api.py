"""V1.4 购物清单接口（11 个端点）。

被测对象：modules/lifeweb/shopping/{schema,crud,router}.py。
本文件只管「正常与异常路径对不对」，**跨用户隔离**另外一份：
tests/test_shopping_isolation.py。

约定（和其它模块的测试一致）：
  - `client` 来自 conftest，走 ASGI 直连，当前用户被钉成 ID 1
  - 要断言「库里到底剩几行」时一律用 `new_session()` 另开会话 ——
    MySQL 默认 REPEATABLE READ，同一个会话会读到旧快照
"""

import json

from sqlalchemy import func, select

from conftest import API, make_expense, make_task, new_session

from core.time import now_utc
from modules.lifeweb.shopping.model import ShoppingItem, ShoppingList
from modules.lifeweb.tasks.model import Task

LISTS = f"{API}/shopping-lists"
ITEMS = f"{API}/shopping-items"


# ---------------------------------------------------------------- 小工具


def _list(client, **body) -> dict:
    """建一张清单，返回响应体。"""
    res = client.post(LISTS, json=body or {"name": "周末采购"})
    assert res.status_code == 201, res.text
    return res.json()


def _item(client, list_id: int, **body) -> dict:
    """往清单里加一条，返回响应体。"""
    res = client.post(f"{LISTS}/{list_id}/items", json=body or {"name": "牛奶"})
    assert res.status_code == 201, res.text
    return res.json()


def _rows(model) -> int:
    """数一张表里还剩几行。"""
    with new_session() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def _load_item(item_id: int) -> ShoppingItem | None:
    with new_session() as s:
        return s.get(ShoppingItem, item_id)


def _load_list(list_id: int) -> ShoppingList | None:
    with new_session() as s:
        return s.get(ShoppingList, list_id)


def _detail(client, list_id: int) -> dict:
    res = client.get(f"{LISTS}/{list_id}")
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------- 清单：列表


def test_empty_list_returns_empty_page(client):
    res = client.get(LISTS)
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["page"] == 1


def test_new_list_shows_two_zero_counts(client):
    """新清单一条明细都没有，两个计数必须是 0 而不是缺字段。"""
    body = _list(client, name="周末采购")
    assert body["item_count"] == 0
    assert body["unpurchased_count"] == 0
    assert body["status"] == "active"
    assert body["note"] is None


def test_counts_are_computed_on_the_server(client):
    """清单列表带着 (总数, 未购数)，而且空清单也在列表里。

    空清单会被 INNER JOIN 吞掉 —— 这条用例就是拦那个回归的。
    """
    empty = _list(client, name="空清单")
    full = _list(client, name="有东西的清单")
    milk = _item(client, full["id"], name="牛奶")
    _item(client, full["id"], name="鸡蛋")
    client.patch(f"{ITEMS}/{milk['id']}/purchased", json={"is_purchased": True})

    items = client.get(LISTS).json()["items"]
    by_id = {row["id"]: row for row in items}

    assert empty["id"] in by_id, "空清单不该从列表里消失"
    assert by_id[empty["id"]]["item_count"] == 0
    assert by_id[full["id"]]["item_count"] == 2
    assert by_id[full["id"]]["unpurchased_count"] == 1


def test_lists_are_paginated_newest_first(client):
    created = [_list(client, name=f"清单{i}") for i in range(3)]

    first = client.get(LISTS, params={"page": 1, "size": 2}).json()
    assert first["total"] == 3
    assert first["pages"] == 2
    assert len(first["items"]) == 2
    # 同一秒创建的靠 id 倒序兜底，所以最新的（清单2）必须在最前
    assert first["items"][0]["id"] == created[-1]["id"]

    second = client.get(LISTS, params={"page": 2, "size": 2}).json()
    assert len(second["items"]) == 1
    assert second["items"][0]["id"] == created[0]["id"]


def test_archived_lists_are_hidden_by_default(client):
    """归档 = 列表里看不到，但数据还在，随时能翻回来。"""
    keep = _list(client, name="留着的")
    gone = _list(client, name="归档的")
    assert client.patch(f"{LISTS}/{gone['id']}", json={"status": "archived"}).status_code == 200

    active = client.get(LISTS).json()["items"]
    assert [row["id"] for row in active] == [keep["id"]]

    archived = client.get(LISTS, params={"status": "archived"}).json()["items"]
    assert [row["id"] for row in archived] == [gone["id"]]

    everything = client.get(LISTS, params={"status": "active"}).json()["items"]
    assert {row["id"] for row in everything} == {keep["id"]}

    # 归档不动明细
    assert _load_list(gone["id"]).status == "archived"


# ---------------------------------------------------------------- 清单：新建 / 校验


def test_create_list_trims_name_and_blank_note_becomes_null(client):
    body = _list(client, name="  周末采购  ", note="   ")
    assert body["name"] == "周末采购"
    assert body["note"] is None


def test_create_list_rejects_blank_name(client):
    assert client.post(LISTS, json={"name": "   "}).status_code == 422
    assert client.post(LISTS, json={"name": ""}).status_code == 422
    assert client.post(LISTS, json={}).status_code == 422


def test_create_list_ignores_client_supplied_user_or_status(client):
    """入参里塞 user_id / status 都不会生效：一个来自令牌，一个是服务端规则。"""
    res = client.post(LISTS, json={"name": "伪装", "user_id": 999, "status": "archived"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "active"
    assert _load_list(body["id"]).user_id == 1


def test_duplicate_list_name_is_409(client):
    """同一个人不能有两张同名清单（唯一索引 uk_shopping_lists_user_name）。"""
    _list(client, name="周末采购")

    res = client.post(LISTS, json={"name": "周末采购"})
    assert res.status_code == 409, res.text
    assert "周末采购" in res.json()["message"]

    with new_session() as s:
        assert s.scalar(
            select(func.count())
            .select_from(ShoppingList)
            .where(ShoppingList.user_id == 1, ShoppingList.name == "周末采购")
        ) == 1


def test_archived_list_frees_its_name_for_rebuild(client):
    """归档后可以重建同名清单 —— 归档那张自动改名成「每日鲜(1)」腾出位置。"""
    first = _list(client, name="每日鲜")
    client.patch(f"{LISTS}/{first['id']}", json={"status": "archived"})

    second = _list(client, name="每日鲜")  # 这次必须成功
    assert second["name"] == "每日鲜"
    assert _load_list(first["id"]).name == "每日鲜(1)"
    assert _load_list(first["id"]).status == "archived"
    assert _load_list(second["id"]).name == "每日鲜"


def test_repeated_archive_numbers_the_copies(client):
    """归档 → 重建 → 再归档，副本依次叫 (1)、(2)。最新的那张永远叫原名。"""
    names = []
    for _ in range(3):
        body = _list(client, name="购物")
        client.patch(f"{LISTS}/{body['id']}", json={"status": "archived"})
        names.append(body["id"])

    current = _list(client, name="购物")
    assert current["name"] == "购物"
    archived = sorted(
        _load_list(list_id).name for list_id in names
    )
    assert archived == ["购物(1)", "购物(2)", "购物(3)"]


def test_archiving_an_archived_copy_does_not_stack_suffixes(client):
    """已经带 (n) 的名字再归档一次，只能换成下一个号，不能叠成「周末(1)(1)」。

    这是第一版改名的真实 bug：直接往后加 (1)，于是「周末(1)」变成「周末(1)(1)」。
    正确行为是先剥掉末尾后缀，再从 1 往上找空位。
    """
    first = _list(client, name="器材")
    client.patch(f"{LISTS}/{first['id']}", json={"status": "archived"})  # 器材(1)
    second = _list(client, name="器材")
    client.patch(f"{LISTS}/{second['id']}", json={"status": "archived"})  # 器材(2)

    # 恢复再归档：名字带后缀进来，出去必须还是「器材(?)」，不能叠加
    client.patch(f"{LISTS}/{second['id']}", json={"status": "active"})
    client.patch(f"{LISTS}/{second['id']}", json={"status": "archived"})

    name = _load_list(second["id"]).name
    assert name.count("(") == 1, name
    assert name in {"器材(1)", "器材(2)", "器材(3)"}, name


def test_only_one_active_list_per_name(client):
    """重建一次之后，再建第二张同名就必须被拦 —— 同一时刻只有一张 active。"""
    first = _list(client, name="省心")
    client.patch(f"{LISTS}/{first['id']}", json={"status": "archived"})
    _list(client, name="省心")

    res = client.post(LISTS, json={"name": "省心"})
    assert res.status_code == 409, res.text


def test_rename_onto_existing_name_is_409(client):
    """改名同样撞唯一索引：PATCH 也得走同一条 409 路径。"""
    _list(client, name="甲")
    second = _list(client, name="乙")

    res = client.patch(f"{LISTS}/{second['id']}", json={"name": "甲"})
    assert res.status_code == 409, res.text
    # 失败不能留下半截状态：名字仍是乙
    assert _load_list(second["id"]).name == "乙"


# ---------------------------------------------------------------- 清单：详情 / 修改 / 删除


def test_detail_returns_list_with_all_items(client):
    body = _list(client, name="周末采购", note="11 月")
    _item(client, body["id"], name="牛奶", quantity="2", unit="箱")
    _item(client, body["id"], name="鸡蛋", quantity="30")

    detail = _detail(client, body["id"])
    assert detail["list"]["id"] == body["id"]
    assert detail["list"]["item_count"] == 2
    assert [item["name"] for item in detail["items"]] == ["牛奶", "鸡蛋"]
    # Decimal 进出参都按字符串走
    assert detail["items"][0]["quantity"] == "2.00"
    assert detail["items"][0]["unit"] == "箱"
    assert detail["items"][1]["unit"] == "个"  # 没传单位自动补


def test_update_list_is_partial(client):
    """三态：没传的不动、传 null 清空、传值改值。"""
    body = _list(client, name="原名", note="原备注")

    renamed = client.patch(f"{LISTS}/{body['id']}", json={"name": "新名"}).json()
    assert renamed["name"] == "新名"
    assert renamed["note"] == "原备注", "只改名字不能把备注冲掉"

    cleared = client.patch(f"{LISTS}/{body['id']}", json={"note": None}).json()
    assert cleared["note"] is None
    assert cleared["name"] == "新名"


def test_update_list_rejects_bad_bodies(client):
    body = _list(client)
    empty = client.patch(f"{LISTS}/{body['id']}", json={})
    assert empty.status_code == 422
    assert "没有需要修改的字段" in empty.text

    null_name = client.patch(f"{LISTS}/{body['id']}", json={"name": None})
    assert null_name.status_code == 422

    bad_status = client.patch(f"{LISTS}/{body['id']}", json={"status": "deleted"})
    assert bad_status.status_code == 422


def test_update_unknown_list_is_404(client):
    assert client.patch(f"{LISTS}/999999", json={"name": "x"}).status_code == 404
    assert client.get(f"{LISTS}/999999").status_code == 404
    assert client.delete(f"{LISTS}/999999").status_code == 404


def test_delete_list_removes_items_with_it(client):
    """删清单 = 连明细一起删，两张表都不许留下孤儿行。"""
    body = _list(client, name="要删的")
    _item(client, body["id"], name="牛奶")
    _item(client, body["id"], name="鸡蛋")
    other = _list(client, name="别动的")
    kept = _item(client, other["id"], name="抽纸")

    assert client.delete(f"{LISTS}/{body['id']}").status_code == 204
    assert _load_list(body["id"]) is None
    assert _rows(ShoppingItem) == 1, "只该剩下另一张清单里的那一条"
    assert _load_item(kept["id"]) is not None

    # 连删两次：第二次 404
    assert client.delete(f"{LISTS}/{body['id']}").status_code == 404


# ---------------------------------------------------------------- 明细：添加


def test_add_item_defaults(client):
    body = _list(client)
    item = _item(client, body["id"], name="  牛奶  ")
    assert item["name"] == "牛奶"
    assert item["quantity"] == "1.00"
    assert item["unit"] == "个"
    assert item["category_id"] == 0
    assert item["category_name"] is None
    assert item["is_purchased"] is False
    assert item["purchased_at"] is None
    assert item["source"] == "manual"


def test_add_item_normalizes_within_category(client):
    """scope=shopping 的系统分类（食品=21）能用，且带上分类名。"""
    body = _list(client)
    item = _item(client, body["id"], name="牛奶", category_id=21)
    assert item["category_id"] == 21
    assert item["category_name"] == "食品"


def test_add_item_rejects_quantity_out_of_range(client):
    body = _list(client)
    for quantity in ("0", "-1", "10000", "1.005"):
        res = client.post(f"{LISTS}/{body['id']}/items", json={"name": "x", "quantity": quantity})
        assert res.status_code == 422, f"quantity={quantity} 应该被拦下"


def test_add_item_rejects_unknown_category(client):
    body = _list(client)
    res = client.post(f"{LISTS}/{body['id']}/items", json={"name": "x", "category_id": 999999})
    assert res.status_code == 422
    assert "分类不存在" in res.text


def test_add_item_rejects_category_from_another_scope(client):
    """分类 1 是 expense/餐饮，不属于购物域，不能挂到购物明细上。"""
    body = _list(client)
    res = client.post(f"{LISTS}/{body['id']}/items", json={"name": "x", "category_id": 1})
    assert res.status_code == 422


def test_add_item_to_unknown_list_is_404(client):
    assert client.post(f"{LISTS}/999999/items", json={"name": "x"}).status_code == 404


def test_sort_order_increases_within_one_list(client):
    body = _list(client)
    orders = [_item(client, body["id"], name=f"东西{i}")["sort_order"] for i in range(3)]
    assert orders == sorted(orders)
    assert len(set(orders)) == 3, "同清单里的 sort_order 不能重复"


def test_item_source_cannot_be_faked(client):
    """客户端传 source=ai 不会生效 —— 页面加的一律 manual。"""
    body = _list(client)
    item = client.post(f"{LISTS}/{body['id']}/items", json={"name": "牛奶", "source": "ai"})
    assert item.status_code == 201
    assert item.json()["source"] == "manual"


# ---------------------------------------------------------------- 明细：查询与筛选


def test_list_items_puts_unpurchased_first(client):
    body = _list(client)
    milk = _item(client, body["id"], name="牛奶")
    egg = _item(client, body["id"], name="鸡蛋")
    _item(client, body["id"], name="抽纸")
    client.patch(f"{ITEMS}/{milk['id']}/purchased", json={"is_purchased": True})

    names = [row["name"] for row in client.get(f"{LISTS}/{body['id']}/items").json()]
    assert names == ["鸡蛋", "抽纸", "牛奶"]

    only_todo = client.get(f"{LISTS}/{body['id']}/items", params={"purchased": False}).json()
    assert [row["name"] for row in only_todo] == ["鸡蛋", "抽纸"]

    only_done = client.get(f"{LISTS}/{body['id']}/items", params={"purchased": True}).json()
    assert [row["name"] for row in only_done] == ["牛奶"]

    assert egg["is_purchased"] is False


def test_list_items_limit_and_unknown_list(client):
    body = _list(client)
    for i in range(3):
        _item(client, body["id"], name=f"东西{i}")

    limited = client.get(f"{LISTS}/{body['id']}/items", params={"limit": 2}).json()
    assert len(limited) == 2
    assert client.get(f"{LISTS}/999999/items").status_code == 404


def test_get_single_item(client):
    body = _list(client)
    item = _item(client, body["id"], name="牛奶", quantity="2.5", unit="斤")
    res = client.get(f"{ITEMS}/{item['id']}")
    assert res.status_code == 200
    assert res.json()["quantity"] == "2.50"
    assert res.json()["unit"] == "斤"
    assert client.get(f"{ITEMS}/999999").status_code == 404


# ---------------------------------------------------------------- 明细：修改


def test_update_item_is_partial(client):
    body = _list(client)
    item = _item(client, body["id"], name="牛奶", quantity="2", note="冷藏")

    updated = client.patch(f"{ITEMS}/{item['id']}", json={"quantity": "5"}).json()
    assert updated["quantity"] == "5.00"
    assert updated["name"] == "牛奶", "只改数量不能把名字冲掉"
    assert updated["note"] == "冷藏"

    cleared = client.patch(f"{ITEMS}/{item['id']}", json={"note": None}).json()
    assert cleared["note"] is None


def test_update_item_rejects_bad_bodies(client):
    body = _list(client)
    item = _item(client, body["id"])

    assert client.patch(f"{ITEMS}/{item['id']}", json={}).status_code == 422
    assert client.patch(f"{ITEMS}/{item['id']}", json={"name": None}).status_code == 422
    assert client.patch(f"{ITEMS}/{item['id']}", json={"quantity": 0}).status_code == 422
    assert (
        client.patch(f"{ITEMS}/{item['id']}", json={"category_id": 999999}).status_code == 422
    )


def test_update_item_cannot_change_purchased_flag(client):
    """is_purchased 不在 ItemUpdate 里，传了也不生效（不能被顺手改坏）。"""
    body = _list(client)
    item = _item(client, body["id"])

    res = client.patch(f"{ITEMS}/{item['id']}", json={"name": "新名", "is_purchased": True})
    assert res.status_code == 200
    assert res.json()["is_purchased"] is False
    assert res.json()["purchased_at"] is None


def test_update_unknown_item_is_404(client):
    assert client.patch(f"{ITEMS}/999999", json={"name": "x"}).status_code == 404


# ---------------------------------------------------------------- 明细：勾选


def test_purchase_sets_purchased_at(client):
    body = _list(client)
    item = _item(client, body["id"])

    done = client.patch(f"{ITEMS}/{item['id']}/purchased", json={"is_purchased": True}).json()
    assert done["is_purchased"] is True
    assert done["purchased_at"] is not None


def test_purchase_is_idempotent(client):
    """重复勾选不刷新 purchased_at，否则「上周买了什么」会漂。"""
    body = _list(client)
    item = _item(client, body["id"])

    first = client.patch(f"{ITEMS}/{item['id']}/purchased", json={"is_purchased": True}).json()
    second = client.patch(f"{ITEMS}/{item['id']}/purchased", json={"is_purchased": True}).json()

    assert second["purchased_at"] == first["purchased_at"]
    assert _load_item(item["id"]).purchased_at is not None


def test_unpurchase_clears_purchased_at(client):
    """取消勾选必须把 purchased_at 置空，不许留下「没买却有购买时间」。"""
    body = _list(client)
    item = _item(client, body["id"])
    client.patch(f"{ITEMS}/{item['id']}/purchased", json={"is_purchased": True})

    undone = client.patch(f"{ITEMS}/{item['id']}/purchased", json={"is_purchased": False}).json()
    assert undone["is_purchased"] is False
    assert undone["purchased_at"] is None
    assert _load_item(item["id"]).purchased_at is None


def test_purchase_updates_list_counters(client):
    body = _list(client)
    milk = _item(client, body["id"], name="牛奶")
    _item(client, body["id"], name="鸡蛋")

    client.patch(f"{ITEMS}/{milk['id']}/purchased", json={"is_purchased": True})
    row = client.get(LISTS).json()["items"][0]
    assert (row["item_count"], row["unpurchased_count"]) == (2, 1)

    client.patch(f"{ITEMS}/{milk['id']}/purchased", json={"is_purchased": False})
    row = client.get(LISTS).json()["items"][0]
    assert (row["item_count"], row["unpurchased_count"]) == (2, 2)


def test_purchase_rejects_missing_flag(client):
    body = _list(client)
    item = _item(client, body["id"])
    assert client.patch(f"{ITEMS}/{item['id']}/purchased", json={}).status_code == 422
    assert (
        client.patch(f"{ITEMS}/{item['id']}/purchased", json={"is_purchased": "maybe"}).status_code
        == 422
    )
    assert (
        client.patch(f"{ITEMS}/999999/purchased", json={"is_purchased": True}).status_code == 404
    )


# ---------------------------------------------------------------- 明细：删除


def test_delete_item_really_removes_row(client):
    body = _list(client)
    item = _item(client, body["id"])
    _item(client, body["id"], name="鸡蛋")

    assert client.delete(f"{ITEMS}/{item['id']}").status_code == 204
    assert _load_item(item["id"]) is None
    assert _rows(ShoppingItem) == 1

    assert client.get(f"{ITEMS}/{item['id']}").status_code == 404
    assert client.delete(f"{ITEMS}/{item['id']}").status_code == 404
    assert client.get(LISTS).json()["items"][0]["item_count"] == 1


# ---------------------------------------------------------------- 互不影响


def test_shopping_does_not_touch_tasks_or_expenses(client):
    """购物模块只碰自己两张表：顺手建一批待办/账目，跑完整套购物操作后它们一行不少。"""
    body = _list(client)
    item = _item(client, body["id"], name="牛奶")

    with new_session() as s:
        make_task(s, title="不该被动")
        make_expense(s, amount="12.30", occurred_at=now_utc())

    client.patch(f"{ITEMS}/{item['id']}/purchased", json={"is_purchased": True})
    client.delete(f"{ITEMS}/{item['id']}")
    client.delete(f"{LISTS}/{body['id']}")

    with new_session() as s:
        assert s.scalar(select(func.count()).select_from(Task)) == 1
    assert _rows(ShoppingList) == 0


def test_response_is_serializable(client):
    """响应必须能被 JSON 序列化（Decimal / datetime 都已经转成字符串）。"""
    body = _list(client)
    _item(client, body["id"])
    detail = _detail(client, body["id"])
    assert json.dumps(detail, ensure_ascii=False)