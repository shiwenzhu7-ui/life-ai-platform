"""A12：ai_pending_actions.action_type 加 update_expense / delete_expense。

MySQL 的 ENUM 改值必须整列重写，所以这里把完整取值列表再写一遍
（与 models/pending_action.py 的 `PENDING_ACTION_TYPES` 严格一致）。
顺序也照抄模型：ENUM 的顺序就是它在库里存编号的顺序，乱序虽然不影响读取，
但 downgrade 时如果少了某个值，那些行会变成空字符串，得小心。

Revision ID: a12pending
Revises: a11session
Create Date: 2026-11-09 17:45:00
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "a12pending"
down_revision: str | Sequence[str] | None = "a11session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AFTER = (
    "create_task",
    "update_task",
    "delete_task",
    "create_expense",
    "update_expense",
    "delete_expense",
    "add_shopping_item",
    "create_shopping_list",
)

_BEFORE = (
    "create_task",
    "update_task",
    "delete_task",
    "create_expense",
    "add_shopping_item",
    "create_shopping_list",
)


def upgrade() -> None:
    # 注意：如果库里已经存在 update_expense / delete_expense 的行，downgrade 会丢信息。
    # 目前只可能在 A12 上线之后才有，正常回滚场景（刚升级就发现不对）是安全的。
    op.alter_column(
        "ai_pending_actions",
        "action_type",
        existing_type=mysql.ENUM(*_BEFORE),
        type_=sa.Enum(*_AFTER, name="pending_action_type"),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "ai_pending_actions",
        "action_type",
        existing_type=sa.Enum(*_AFTER, name="pending_action_type"),
        type_=mysql.ENUM(*_BEFORE),
        existing_nullable=False,
    )