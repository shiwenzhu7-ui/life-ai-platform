"""seed reference data

写入系统预置分类（7 个用途域共 41 条）与固定演示用户 id=1。

Revision ID: d7cb9a6fe302
Revises: 5eca81543f56
Create Date: 2026-10-30 11:03:27.227152

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7cb9a6fe302"
down_revision: str | Sequence[str] | None = "5eca81543f56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DEMO_USER_ID = 1

# 系统预置一级分类（owner_user_id=0, is_system=1）
# 顺序即 sort_order，前端下拉框按此展示
SYSTEM_CATEGORIES: dict[str, list[str]] = {
    "expense": ["餐饮", "交通", "购物", "居住", "娱乐", "医疗", "教育", "人情", "其他"],
    "income": ["工资", "奖金", "兼职", "理财", "其他"],
    "task": ["工作", "学习", "生活", "购物", "健康", "其他"],
    "shopping": ["食品", "日用品", "数码", "服饰", "其他"],
    "diet": ["早餐", "午餐", "晚餐", "加餐"],
    "exercise": ["跑步", "力量", "球类", "游泳", "拉伸", "其他"],
    "mood": ["开心", "平静", "低落", "焦虑", "疲惫", "生气"],
}

categories_table = sa.table(
    "categories",
    sa.column("scope", sa.String),
    sa.column("name", sa.String),
    sa.column("parent_id", sa.SmallInteger),
    sa.column("owner_user_id", sa.SmallInteger),
    sa.column("is_system", sa.SmallInteger),
    sa.column("sort_order", sa.SmallInteger),
)

users_table = sa.table(
    "users",
    sa.column("id", sa.BigInteger),
    sa.column("username", sa.String),
    sa.column("nickname", sa.String),
    sa.column("role", sa.String),
    sa.column("auth_source", sa.String),
    sa.column("daily_ai_quota", sa.Integer),
)


def upgrade() -> None:
    """写入系统分类与演示用户。"""
    rows: list[dict] = []
    for scope, names in SYSTEM_CATEGORIES.items():
        for index, name in enumerate(names, start=1):
            rows.append(
                {
                    "scope": scope,
                    "name": name,
                    "parent_id": 0,
                    "owner_user_id": 0,
                    "is_system": 1,
                    "sort_order": index,
                }
            )
    op.bulk_insert(categories_table, rows)

    op.bulk_insert(
        users_table,
        [
            {
                "id": DEMO_USER_ID,
                "username": "demo",
                "nickname": "演示用户",
                "role": "user",
                "auth_source": "local",
                "daily_ai_quota": 100,
            }
        ],
    )


def downgrade() -> None:
    """回滚 seed：只删本次插入的数据。"""
    op.execute(
        sa.text(
            "DELETE FROM categories WHERE is_system = 1 AND owner_user_id = 0 AND parent_id = 0"
        )
    )
    op.execute(sa.text(f"DELETE FROM users WHERE id = {DEMO_USER_ID}"))
