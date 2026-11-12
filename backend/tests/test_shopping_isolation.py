"""V1.4 购物的跨用户数据隔离。

V1.2 的验收项「两用户互不可见」在购物模块的落地版（对照 tests/test_user_isolation.py）。
这里多一层麻烦：购物是**父子两层**，所以每个越权动作要确认两件事 ——
  1. 返回 404（不是 403：403 等于承认「这条存在」）
  2. 库里那一行一个字都没变

conftest 把当前用户钉成 1 号，本文件用 `acting_as()` 冒充别人。
"""

from contextlib import contextmanager

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conftest import API, engine, new_session

from app import app
from core.deps import get_current_user_id
from modules.lifeweb.shopping.model import ShoppingItem, ShoppingList
from modules.shared.users.model import User

ME, OTHER = 1, 2

LISTS = f"{API}/shopping-lists"
ITEMS = f"{API}/shopping-items"


@contextmanager
def acting_as(user_id: int):
    """临时冒充某个用户。退出时务必还原成 1 号。"""
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    try:
        yield
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: ME


@pytest.fixture()
def two_users():
    """1 号和 2 号必须真实存在。"""
    with new_session() as s:
        s.add_all(
            [
                User(id=ME, username="me", nickname="我", role="user", status="active"),
                User(id=OTHER, username="other", nickname="他", role="user", status="active"),
            ]
        )
        s.commit()
    yield ME, OTHER


def _seed(user_id: int, list_name: str, item_names: list[str]) -> tuple[int, list[int]]:
    """直接写库造一套「清单 + 明细」，返回 (list_id, [item_id, ...])。"""
    with Session(engine) as s:
        shopping_list = ShoppingList(user_id=user_id, name=list_name, status="active")
        s.add(shopping_list)
        s.flush()
        items = []
        for index, name in enumerate(item_names):
            item = ShoppingItem(
                list_id=shopping_list.id,
                user_id=user_id,
                name=name,
                quantity=1,
                unit="个",
                sort_order=index,
                source="manual",
            )
            s.add(item)
            items.append(item)
        s.commit()
        return shopping_list.id, [item.id for item in items]


def _item_row(item_id: int) -> ShoppingItem | None:
    with new_session() as s:
        return s.get(ShoppingItem, item_id)


def _list_row(list_id: int) -> ShoppingList | None:
    with new_session() as s:
        return s.get(ShoppingList, list_id)


# ---------------------------------------------------------------- 列表


def test_list_only_contains_my_lists(client, two_users):
    mine = _seed(ME, "我的清单", ["牛奶"])
    theirs = _seed(OTHER, "他的清单", ["鸡蛋"])

    with acting_as(ME):
        ids = [row["id"] for row in client.get(LISTS).json()["items"]]
    assert ids == [mine[0]]

    with acting_as(OTHER):
        ids = [row["id"] for row in client.get(LISTS).json()["items"]]
    assert ids == [theirs[0]]


def test_counts_do_not_leak_across_users(client, two_users):
    """计数子查询也按 user_id 分组 —— 别人清单里的条数不会算到我头上。"""
    mine = _seed(ME, "我的清单", ["牛奶"])
    _seed(OTHER, "他的清单", ["鸡蛋", "抽纸", "酱油"])

    with acting_as(ME):
        row = client.get(LISTS).json()["items"][0]
    assert row["id"] == mine[0]
    assert (row["item_count"], row["unpurchased_count"]) == (1, 1)


# ---------------------------------------------------------------- 清单：读 / 改 / 删


def test_cannot_read_someone_elses_list(client, two_users):
    theirs = _seed(OTHER, "他的清单", ["鸡蛋"])
    with acting_as(ME):
        assert client.get(f"{LISTS}/{theirs[0]}").status_code == 404
        assert client.get(f"{LISTS}/{theirs[0]}/items").status_code == 404


def test_cannot_rename_someone_elses_list(client, two_users):
    theirs = _seed(OTHER, "他的清单", ["鸡蛋"])
    with acting_as(ME):
        assert client.patch(f"{LISTS}/{theirs[0]}", json={"name": "偷"}).status_code == 404
    assert _list_row(theirs[0]).name == "他的清单"


def test_cannot_archive_someone_elses_list(client, two_users):
    theirs = _seed(OTHER, "他的清单", [])
    with acting_as(ME):
        assert client.patch(f"{LISTS}/{theirs[0]}", json={"status": "archived"}).status_code == 404
    assert _list_row(theirs[0]).status == "active"


def test_cannot_delete_someone_elses_list(client, two_users):
    theirs = _seed(OTHER, "他的清单", ["鸡蛋", "抽纸"])
    with acting_as(ME):
        assert client.delete(f"{LISTS}/{theirs[0]}").status_code == 404
    assert _list_row(theirs[0]) is not None
    assert _item_row(theirs[1][0]) is not None, "明细也不许被连带删掉"


# ---------------------------------------------------------------- 明细


def test_cannot_add_item_into_someone_elses_list(client, two_users):
    """知道一个 list_id 就能往别人清单里塞东西 —— 这个必须 404。"""
    theirs = _seed(OTHER, "他的清单", [])
    with acting_as(ME):
        res = client.post(f"{LISTS}/{theirs[0]}/items", json={"name": "偷偷加的"})
        assert res.status_code == 404

    with new_session() as s:
        count = s.scalar(
            select(func.count()).select_from(ShoppingItem).where(ShoppingItem.list_id == theirs[0])
        )
    assert count == 0


def test_cannot_read_someone_elses_item(client, two_users):
    _seed(ME, "我的清单", [])
    theirs = _seed(OTHER, "他的清单", ["鸡蛋"])
    with acting_as(ME):
        assert client.get(f"{ITEMS}/{theirs[1][0]}").status_code == 404


def test_cannot_modify_someone_elses_item(client, two_users):
    _seed(ME, "我的清单", [])
    theirs = _seed(OTHER, "他的清单", ["鸡蛋"])
    item_id = theirs[1][0]

    with acting_as(ME):
        assert client.patch(f"{ITEMS}/{item_id}", json={"name": "偷"}).status_code == 404
        assert (
            client.patch(f"{ITEMS}/{item_id}", json={"quantity": "99"}).status_code == 404
        )

    row = _item_row(item_id)
    assert row.name == "鸡蛋"
    assert str(row.quantity) in {"1.00", "1"}


def test_cannot_tick_someone_elses_item(client, two_users):
    _seed(ME, "我的清单", [])
    theirs = _seed(OTHER, "他的清单", ["鸡蛋"])
    item_id = theirs[1][0]

    with acting_as(ME):
        res = client.patch(f"{ITEMS}/{item_id}/purchased", json={"is_purchased": True})
        assert res.status_code == 404

    row = _item_row(item_id)
    assert row.is_purchased == 0
    assert row.purchased_at is None


def test_cannot_delete_someone_elses_item(client, two_users):
    _seed(ME, "我的清单", [])
    theirs = _seed(OTHER, "他的清单", ["鸡蛋"])
    item_id = theirs[1][0]

    with acting_as(ME):
        assert client.delete(f"{ITEMS}/{item_id}").status_code == 404

    assert _item_row(item_id) is not None


def test_same_item_name_is_not_shared(client, two_users):
    """两家都有「牛奶」，各查各的，不串。"""
    mine = _seed(ME, "我的清单", ["牛奶"])
    theirs = _seed(OTHER, "他的清单", ["牛奶"])

    with acting_as(ME):
        client.patch(f"{ITEMS}/{mine[1][0]}/purchased", json={"is_purchased": True})

    assert _item_row(mine[1][0]).is_purchased == 1
    assert _item_row(theirs[1][0]).is_purchased == 0


# ---------------------------------------------------------------- 双向确认


def test_both_directions_are_blocked(client, two_users):
    """1 号看不了 2 号的，2 号也看不了 1 号的 —— 不是单向过滤。"""
    mine = _seed(ME, "我的清单", ["牛奶"])
    theirs = _seed(OTHER, "他的清单", ["鸡蛋"])

    with acting_as(OTHER):
        assert client.get(f"{LISTS}/{mine[0]}").status_code == 404
        assert client.patch(f"{ITEMS}/{mine[1][0]}", json={"name": "偷"}).status_code == 404
        assert client.delete(f"{LISTS}/{mine[0]}").status_code == 404

    with acting_as(ME):
        assert client.get(f"{LISTS}/{theirs[0]}").status_code == 404

    assert _list_row(mine[0]).name == "我的清单"
    assert _item_row(mine[1][0]).name == "牛奶"