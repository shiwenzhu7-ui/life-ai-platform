"""分类接口：V1.0 只有一个只读接口，但它是筛选条与「记一笔」的数据源。

两个必须守住的点：
- 只返回「自己的 + 系统的」，别人的自定义分类不能出现；
- 字段固定 6 个，`owner_user_id` / `is_system` 不能漏出去（否则前端会拿它当权限判断）。
"""

from conftest import API
from sqlalchemy.orm import Session

from core.db import engine
from modules.shared.categories.model import Category


def test_list_categories_by_scope(client):
    body = client.get(f"{API}/categories", params={"scope": "task"}).json()

    assert [item["name"] for item in body] == ["工作", "学习", "生活", "购物", "健康", "其他"]
    assert [item["sort_order"] for item in body] == [1, 2, 3, 4, 5, 6]


def test_list_categories_returns_only_public_fields(client):
    item = client.get(f"{API}/categories", params={"scope": "task"}).json()[0]

    assert set(item) == {"id", "scope", "name", "parent_id", "icon", "sort_order"}


def test_list_categories_without_scope_returns_all_41(client):
    body = client.get(f"{API}/categories").json()

    assert len(body) == 41


def test_list_categories_rejects_unknown_scope(client):
    res = client.get(f"{API}/categories", params={"scope": "bogus"})

    assert res.status_code == 422
    assert res.json()["code"] == "VALIDATION_ERROR"


def test_list_categories_hides_other_users_custom_category(client):
    with Session(engine) as session:
        session.add(
            Category(
                scope="task",
                name="别人的私货",
                parent_id=0,
                owner_user_id=999,
                is_system=0,
                sort_order=99,
            )
        )
        session.commit()

    names = [item["name"] for item in client.get(f"{API}/categories", params={"scope": "task"}).json()]

    assert "别人的私货" not in names


def test_list_categories_includes_own_custom_category(client):
    with Session(engine) as session:
        session.add(
            Category(
                scope="task",
                name="我的私货",
                parent_id=0,
                owner_user_id=1,
                is_system=0,
                sort_order=99,
            )
        )
        session.commit()

    names = [item["name"] for item in client.get(f"{API}/categories", params={"scope": "task"}).json()]

    assert "我的私货" in names


def test_list_categories_skips_soft_deleted(client):
    with Session(engine) as session:
        row = Category(
            scope="task",
            name="删掉的",
            parent_id=0,
            owner_user_id=1,
            is_system=0,
            sort_order=98,
        )
        session.add(row)
        session.commit()
        from core.time import now_utc

        row.deleted_at = now_utc()
        session.commit()

    names = [item["name"] for item in client.get(f"{API}/categories", params={"scope": "task"}).json()]

    assert "删掉的" not in names
