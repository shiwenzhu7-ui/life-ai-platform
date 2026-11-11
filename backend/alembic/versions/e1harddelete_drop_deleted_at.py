"""V1.3：删除语义改为硬删 —— 从 tasks / expenses / documents 去掉 deleted_at

Revision ID: e1harddelete
Revises: d1knowledge
Create Date: 2026-11-11 09:00:00

决策与理由：

- `tasks` / `expenses` / `documents` 三张表的删除改成 **DELETE**，行真的消失。
  软删要求「每一处查询都记得补 `deleted_at IS NULL`」，漏一处就是把删掉的
  数据算进统计里；硬删把这件事变成数据库保证的，不靠人记。
- `chat_sessions`、`categories`、`users`、`shopping_*` 的 `deleted_at` **保留**：
  - chat_sessions：对话历史对排查很有用，用户明确要求保留软删（2026-11-11）
  - categories / users / shopping_*：目前没有删除入口，列留着，等真做管理后台再决定

⚠️ 不可逆：已经软删的旧行（`deleted_at IS NOT NULL`）在 upgrade 时一并
DELETE 掉，因为这些行在业务上就是「已删除」的数据，把列去掉之后它们会
凭空「复活」出现在列表里 —— 那比丢掉它们更糟。文件与 Milvus 向量不在这里清理，
dev 库里那几条残留属于「安全垃圾」。

幂等：每一列都先查 information_schema 再删，重复执行不会报错。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1harddelete"
down_revision: str | Sequence[str] | None = "d1knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 去掉 deleted_at 的三张表
_TABLES = ("tasks", "expenses", "documents")


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
    bind = op.get_bind()
    for table in _TABLES:
        if not _column_exists(table, "deleted_at"):
            continue
        # 先清掉已软删的行：列一去掉，它们就没有「已删除」的标记了，
        # 会重新出现在列表和统计里 —— 那不是用户的意思
        bind.execute(sa.text(f"DELETE FROM `{table}` WHERE deleted_at IS NOT NULL"))
        op.drop_column(table, "deleted_at")


def downgrade() -> None:
    # 只能把列建回来（空值 = 未删除）。被物理删掉的行回不来，这是硬删的代价。
    for table in _TABLES:
        if _column_exists(table, "deleted_at"):
            continue
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(), nullable=True))