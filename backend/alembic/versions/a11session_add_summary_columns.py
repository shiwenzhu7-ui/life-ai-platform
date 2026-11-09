"""A11：给 chat_sessions 加前情摘要的两列。

`summary` 用 MEDIUMTEXT（摘要会随对话变长，虽然限制了 300 字，留足余量比
`String(1000)` 出问题后改表省事）；`summary_until_id` 记到哪条消息为止。

Revision ID: a11session
Revises: 6e343c615ded
Create Date: 2026-11-09 16:15:00
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "a11session"
down_revision: str | Sequence[str] | None = "6e343c615ded"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("summary", mysql.MEDIUMTEXT(), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("summary_until_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "summary_until_id")
    op.drop_column("chat_sessions", "summary")