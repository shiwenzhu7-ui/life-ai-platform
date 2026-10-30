"""测试夹具。

取舍说明：V1.0 直接连本机 lifeai 库测试（独立测试库留到 V1.4 完善）。
每个用例前后清空演示用户的待办，保证用例之间互不干扰。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.deps import DEMO_USER_ID
from app.db.session import SessionLocal
from app.main import app
from app.models import Category, Task


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


def _purge_tasks() -> None:
    db = SessionLocal()
    try:
        db.execute(delete(Task).where(Task.user_id == DEMO_USER_ID))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _clean_tasks():
    _purge_tasks()
    yield
    _purge_tasks()


@pytest.fixture(scope="session")
def task_category_ids() -> dict[str, int]:
    """待办域的分类 name → id 映射，供用例构造数据。"""
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Category.name, Category.id).where(Category.scope == "task")
        ).all()
        return {name: category_id for name, category_id in rows}
    finally:
        db.close()
