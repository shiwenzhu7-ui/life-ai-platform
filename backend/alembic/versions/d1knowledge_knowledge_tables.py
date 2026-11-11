"""V1.3：知识库三张表（documents / document_chunks / async_tasks）

Revision ID: d1knowledge
Revises: c1noquota
Create Date: 2026-11-10 19:05:00

对应 `docs/数据库设计.md` §3.14 / §3.15 / §3.21，迁移表里的 `006_knowledge_base`。

与设计文档的三处偏差，都是有意为之：

1. `documents.user_id` 不设 `DEFAULT 0`，也不做「公共知识」。
   文档里写着 `0 = 公共知识（仅 admin 创建）`，但管理后台一直没落地，
   这个语义写不进去任何一条真实数据。列 `visibility` 照建，V1.3 恒为 'private'。
2. `document_chunks.vector_id` 保留但恒为 NULL —— Milvus 主键直接取本表 id
   （见 `core/vectorstore.py`），不需要额外的映射列。
3. `document_chunks.token_count` 存的是**字符数**而不是 token 数：
   嵌入模型走 fastembed，不暴露分词计数。

幂等：upgrade 前逐表查 information_schema，已存在就跳过。
downgrade 按依赖倒序删（chunks → documents → async_tasks 无所谓，三表无外键）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1knowledge"
down_revision: str | Sequence[str] | None = "c1noquota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": table},
    ).scalar()
    return bool(row)


def upgrade() -> None:
    if not _table_exists("documents"):
        op.create_table(
            "documents",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "visibility",
                sa.Enum("private", "public", name="doc_visibility"),
                nullable=False,
                server_default="private",
            ),
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("stored_path", sa.String(500), nullable=False),
            sa.Column("file_ext", sa.String(10), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column(
                "status",
                sa.Enum(
                    "pending",
                    "parsing",
                    "chunking",
                    "embedding",
                    "ready",
                    "failed",
                    name="doc_status",
                ),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_message", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                onupdate=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("user_id", "sha256", name="uk_documents_user_sha"),
        )
        op.create_index("idx_documents_user_status", "documents", ["user_id", "status"])

    if not _table_exists("document_chunks"):
        op.create_table(
            "document_chunks",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("document_id", sa.BigInteger(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("page_no", sa.Integer(), nullable=True),
            sa.Column("vector_id", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("document_id", "chunk_index", name="uk_chunks_doc_index"),
        )
        op.create_index("idx_chunks_user", "document_chunks", ["user_id"])
        op.create_index("idx_chunks_doc", "document_chunks", ["document_id"])

    if not _table_exists("async_tasks"):
        op.create_table(
            "async_tasks",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "task_type",
                sa.Enum(
                    "doc_parse",
                    "doc_embed",
                    "report_gen",
                    "cleanup",
                    "remind",
                    name="async_task_type",
                ),
                nullable=False,
            ),
            sa.Column("ref_id", sa.BigInteger(), nullable=True),
            sa.Column(
                "status",
                sa.Enum("queued", "running", "success", "failed", name="async_status"),
                nullable=False,
                server_default="queued",
            ),
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("message", sa.String(255), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                onupdate=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("idx_async_status", "async_tasks", ["task_type", "status"])
        op.create_index("idx_async_user", "async_tasks", ["user_id", "created_at"])


def downgrade() -> None:
    # 顺序：先删依赖方。三表没有外键，顺序其实无所谓，
    # 但按「chunks 依赖 documents」的语义写更不容易出错
    if _table_exists("async_tasks"):
        op.drop_table("async_tasks")
    if _table_exists("document_chunks"):
        op.drop_table("document_chunks")
    if _table_exists("documents"):
        op.drop_table("documents")