"""建 categories 表 + tasks 加 category_id + seed 41 条系统分类

Revision ID: 482d5e0a4da8
Revises: a553bb786492
Create Date: 2026-11-01 15:36:13.757513

这一步做三件事：
  1. 建 `categories` 分类字典表（7 个用途域共用一张表，靠 scope 区分）
  2. 给 `tasks` 加 `category_id`（SMALLINT UNSIGNED NOT NULL DEFAULT 0，0=未分类）
     为什么能直接加非空列：给了 server_default='0'，MySQL 会把已有的 11 行填成 0，
     不会像「加非空无默认值的列」那样报错。
  3. 写入 41 条系统预置分类（owner_user_id=0, is_system=1），
     数据出处：docs/数据库设计.md §3.2 的 seed 表。

回滚（downgrade）会删表删列，那 41 条 seed 随表一起消失，不用单独删。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "482d5e0a4da8"
down_revision: str | Sequence[str] | None = "a553bb786492"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 系统预置一级分类（owner_user_id=0, is_system=1, parent_id=0）
# 顺序即 sort_order，前端下拉框按此展示，不要随手调换
SYSTEM_CATEGORIES: dict[str, list[str]] = {
    "expense": ["餐饮", "交通", "购物", "居住", "娱乐", "医疗", "教育", "人情", "其他"],
    "income": ["工资", "奖金", "兼职", "理财", "其他"],
    "task": ["工作", "学习", "生活", "购物", "健康", "其他"],
    "shopping": ["食品", "日用品", "数码", "服饰", "其他"],
    "diet": ["早餐", "午餐", "晚餐", "加餐"],
    "exercise": ["跑步", "力量", "球类", "游泳", "拉伸", "其他"],
    "mood": ["开心", "平静", "低落", "焦虑", "疲惫", "生气"],
}

# 只声明要写的列：bulk_insert 按 dict 的键生成 INSERT，
# 没声明的列（id / created_at / updated_at）走数据库默认值
categories_table = sa.table(
    "categories",
    sa.column("scope", sa.String),
    sa.column("name", sa.String),
    sa.column("parent_id", sa.SmallInteger),
    sa.column("owner_user_id", sa.BigInteger),
    sa.column("is_system", sa.SmallInteger),
    sa.column("sort_order", sa.Integer),
)


def upgrade() -> None:
    """建表 → 加列 → 写 seed。"""
    op.create_table(
        "categories",
        sa.Column("id", mysql.SMALLINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "scope",
            sa.Enum(
                "expense",
                "income",
                "task",
                "shopping",
                "diet",
                "exercise",
                "mood",
                name="category_scope",
            ),
            nullable=False,
            comment="用途域：expense/income/task/...",
        ),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column(
            "parent_id",
            mysql.SMALLINT(unsigned=True),
            server_default="0",
            nullable=False,
            comment="0=一级分类",
        ),
        sa.Column(
            "owner_user_id",
            mysql.BIGINT(unsigned=True),
            server_default="0",
            nullable=False,
            comment="0=系统预置",
        ),
        sa.Column(
            "is_system",
            mysql.TINYINT(display_width=1),
            server_default="0",
            nullable=False,
            comment="1=系统预置不可删",
        ),
        sa.Column("icon", sa.String(length=32), nullable=True),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment="同 scope 内的展示顺序，从 1 开始",
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True, comment="软删除时间，NULL 表示未删除"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope",
            "owner_user_id",
            "parent_id",
            "name",
            name="uk_categories_scope_owner_parent_name",
        ),
    )
    op.create_index("idx_categories_scope_sort", "categories", ["scope", "sort_order"], unique=False)

    op.add_column(
        "tasks",
        sa.Column(
            "category_id",
            mysql.SMALLINT(unsigned=True),
            server_default="0",
            nullable=False,
            comment="关联 categories.id，0=未分类",
        ),
    )

    rows: list[dict] = []
    for scope, names in SYSTEM_CATEGORIES.items():
        for order, name in enumerate(names, start=1):
            rows.append(
                {
                    "scope": scope,
                    "name": name,
                    "parent_id": 0,
                    "owner_user_id": 0,
                    "is_system": 1,
                    "sort_order": order,
                }
            )
    op.bulk_insert(categories_table, rows)


def downgrade() -> None:
    """删列 → 删索引 → 删表（41 条 seed 随表一起消失）。"""
    op.drop_column("tasks", "category_id")
    op.drop_index("idx_categories_scope_sort", table_name="categories")
    op.drop_table("categories")
