"""V1.2 收尾：删掉 users.daily_ai_quota（取消 AI 每日调用额度）

Revision ID: c1noquota
Revises: b1users
Create Date: 2026-11-10 17:20:00

背景：
    用户明确要求「无限制使用次数」，不要每天 50 次的限制。
    这一列是 V1.2 建表时按文档预埋的额度口径（`docs/数据库设计.md:67`），
    代码从来没有真正拦截过调用。既然确定不做这个功能，就把列删掉，
    不留「看起来有额度、实际不管」的半成品 —— 那种状态最容易让以后的人误会。

为什么不直接 ALTER TABLE：
    加列时是逐个查 information_schema 补的（迁移 b1users 的幂等做法），
    删列同理 —— 列不存在就跳过，重复执行不报错，也不依赖「上一步一定成功」。

回滚（downgrade）：
    把列加回来，NOT NULL DEFAULT 50（与 b1users 建列时一致）。
    但原来每个用户的个性化额度（演示用户是 100）回不来，一律变 50 ——
    额度功能已经取消，这个代价可以接受。真要恢复，得从备份里取。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1noquota"
down_revision: str | Sequence[str] | None = "b1users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).scalar()
    return bool(row)


def upgrade() -> None:
    if _column_exists("users", "daily_ai_quota"):
        op.drop_column("users", "daily_ai_quota")


def downgrade() -> None:
    if not _column_exists("users", "daily_ai_quota"):
        op.add_column(
            "users",
            sa.Column(
                "daily_ai_quota",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("50"),
            ),
        )