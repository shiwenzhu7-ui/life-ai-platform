"""测试基座。

三条约定：

1. **只连测试库**：把 `.env` 里的 `DATABASE_URL` 改名为 `lifeai_test`，
   在 import 任何业务模块之前写回环境变量，所以 `core.db` 的 engine 天然指向测试库。
   真实库 `lifeai` 在这些测试里一次都不会被打开。
2. **每个用例前后清空业务表**：不用事务回滚（TestClient 会在别的线程里用连接，
   回滚方案容易踩线程/连接绑定的坑），改成「删光 + 重新插 41 条系统分类」。
   表很小，一次清空不到 10ms。
3. **结构以模型为准**：session 级 fixture 先 `drop_all` 再 `create_all`，
   所以测试库的表结构永远等于 `Base.metadata`，不需要跑 Alembic。
"""

import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB = "lifeai_test"

_raw_url = dotenv_values(BACKEND_DIR / ".env").get("DATABASE_URL", "")
if not _raw_url:
    raise RuntimeError("backend/.env 里没有 DATABASE_URL，测试无法确定连哪个 MySQL")

_url = make_url(_raw_url)
TEST_URL = _url.set(database=TEST_DB)
# 必须在 import core.config / core.db 之前写回环境变量
os.environ["DATABASE_URL"] = TEST_URL.render_as_string(hide_password=False)

import pymysql  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import modules.registry  # noqa: E402,F401  导入即注册全部表
from app import app  # noqa: E402
from core.db import Base, engine  # noqa: E402
from modules.shared.categories.model import Category  # noqa: E402

API = "/api/v1"

# 与 alembic 迁移 `482d5e0a4da8` 里的种子一致（顺序即 id）：7 个域共 41 条
SYSTEM_CATEGORIES: list[tuple[str, str]] = [
    ("expense", "餐饮"),
    ("expense", "交通"),
    ("expense", "购物"),
    ("expense", "居住"),
    ("expense", "娱乐"),
    ("expense", "医疗"),
    ("expense", "教育"),
    ("expense", "人情"),
    ("expense", "其他"),
    ("income", "工资"),
    ("income", "奖金"),
    ("income", "兼职"),
    ("income", "理财"),
    ("income", "其他"),
    ("task", "工作"),
    ("task", "学习"),
    ("task", "生活"),
    ("task", "购物"),
    ("task", "健康"),
    ("task", "其他"),
    ("shopping", "食品"),
    ("shopping", "日用品"),
    ("shopping", "数码"),
    ("shopping", "服饰"),
    ("shopping", "其他"),
    ("diet", "早餐"),
    ("diet", "午餐"),
    ("diet", "晚餐"),
    ("diet", "加餐"),
    ("exercise", "跑步"),
    ("exercise", "力量"),
    ("exercise", "球类"),
    ("exercise", "游泳"),
    ("exercise", "拉伸"),
    ("exercise", "其他"),
    ("mood", "开心"),
    ("mood", "平静"),
    ("mood", "低落"),
    ("mood", "焦虑"),
    ("mood", "疲惫"),
    ("mood", "生气"),
]


def _ensure_database() -> None:
    """测试库不存在就建一个（只建库，不建表）。"""
    conn = pymysql.connect(
        host=_url.host or "127.0.0.1",
        port=_url.port or 3306,
        user=_url.username,
        password=_url.password or "",
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{TEST_DB}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
        conn.commit()
    finally:
        conn.close()


def _clear_tables() -> None:
    names = [t.name for t in Base.metadata.sorted_tables]
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for name in names:
            conn.execute(text(f"DELETE FROM `{name}`"))
            # 自增也归零：分类 id 是测试里的固定期待值（15 = 工作），
            # 只删行不重置自增的话，第二轮 id 就变成 42 起步了。
            conn.execute(text(f"ALTER TABLE `{name}` AUTO_INCREMENT = 1"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


def _seed_categories() -> None:
    # sort_order 是「每个域内从 1 开始」，与生产库一致（不是全局序号）
    counters: dict[str, int] = {}
    rows = []
    for scope, name in SYSTEM_CATEGORIES:
        counters[scope] = counters.get(scope, 0) + 1
        rows.append(
            Category(
                scope=scope,
                name=name,
                parent_id=0,
                owner_user_id=0,
                is_system=1,
                sort_order=counters[scope],
            )
        )
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()


_ensure_database()


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """整个测试会话开始时把测试库结构刷成与模型一致。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def _clean_db():
    """每个用例前后都清空业务表，并重新插入 41 条系统分类。"""
    _clear_tables()
    _seed_categories()
    yield
    _clear_tables()


@pytest.fixture()
def client():
    """FastAPI 测试客户端（同进程直连 ASGI，不走真实 HTTP）。"""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db():
    """直连测试库的会话，用于「接口之外」的断言（比如软删后行是否还在）。"""
    with Session(engine) as session:
        yield session


@contextmanager
def new_session():
    """另开一个全新会话。

    用途：MySQL 默认 REPEATABLE READ，同一个会话里「先写、再被接口改、再读」会读到旧快照，
    所以凡是要在接口调用之后查库的断言，都另开会话，保证看到已提交的最新数据。
    """
    with Session(engine) as session:
        yield session


def make_task(
    db: Session,
    *,
    user_id: int = 1,
    title: str = "测试待办",
    status: str = "todo",
    priority: str = "medium",
    due_at=None,
    category_id: int = 0,
    description: str | None = None,
    completed_at=None,
):
    """直接写一条待办（等价于库里的行，不经过接口）。"""
    from modules.lifeweb.tasks.model import Task

    task = Task(
        user_id=user_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        due_at=due_at,
        completed_at=completed_at,
        category_id=category_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def make_expense(
    db: Session,
    *,
    user_id: int = 1,
    type_: str = "expense",
    amount="10.00",
    category_id: int = 1,
    account: str = "现金",
    occurred_at=None,
    note: str | None = None,
    source: str = "manual",
    deleted_at=None,
):
    """直接写一条收支（不经过接口）。"""
    from modules.lifeweb.expenses.model import Expense

    expense = Expense(
        user_id=user_id,
        type=type_,
        amount=amount,
        category_id=category_id,
        account=account,
        occurred_at=occurred_at,
        note=note,
        source=source,
        deleted_at=deleted_at,
    )
    db.add(expense)
    db.commit()
    db.refresh(expense)
    return expense
