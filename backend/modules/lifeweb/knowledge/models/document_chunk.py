"""document_chunks 表（文档分块正文）。

设计依据 `docs/数据库设计.md` §3.15，迁移 `d1knowledge`。

**正文只存这里，不进 Milvus。** 这是设计文档的硬规矩，也是实践上的正确选择：
Milvus 的标量字段查询能力弱、改一条正文要重建索引；而 MySQL 的 TEXT 足够，
检索时先用 Milvus 拿 id，再按 id 回表取正文，多一次 IN 查询而已。

`id` 同时用作 Milvus 的主键（见 `core/vectorstore.py`），所以：
- 删文档 → 按 document_id 查出 chunk id 列表 → `client.delete(ids=[...])`，一步到位
- 不需要 `vector_id` 这个第三方映射（设计文档有这列，这里保留但恒为 NULL，见下）

为什么不加外键（与 tasks/expenses 一致）：
    Alembic 删文档时先删 chunks 再删 documents，顺序由代码保证；
    加了外键反而让分批删除、数据修复变麻烦。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        # 同一文档内序号唯一：重新切块时靠它判断「这条是不是已经入库了」，
        # 也让切块逻辑的 bug（比如重复段）在数据库层直接暴露
        UniqueConstraint("document_id", "chunk_index", name="uk_chunks_doc_index"),
        # 检索结果回表取正文：拿到的是一串 id → WHERE id IN (...)。
        # 主键本身就够用，这个索引是给「删文档时按 document_id 批量删」用的
        Index("idx_chunks_user", "user_id"),
        Index("idx_chunks_doc", "document_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 冗余一份 user_id：Milvus 里也存这个字段做隔离过滤，
    # 出事时（比如两边数据错位）可以直接对上，不用多一次 JOIN
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 文档内序号，从 0 开始。切分时按顺序编号，重新切块也按它覆盖
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 设计文档叫 token_count，但我们用 fastembed 不暴露分词计数。
    # 这里存**字符数**（len(content)），作为「这段多长」的参考值 ——
    # 别再按 token 去理解它，改名会牵动文档，就在这儿说清楚。
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # PDF 才有页码（pypdf 按页读），DOCX/TXT 为 NULL。
    # 引用来源卡片显示「第 3 页」就是靠它
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 设计文档里有这一列，表示 Milvus 主键。
    # V1.3 让 Milvus 主键直接等于本表 id，所以恒为 NULL ——
    # 保留列是为了将来若改用 Milvus 自增主键，不必再迁移一次表结构
    vector_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DocumentChunk id={self.id} doc={self.document_id} #{self.chunk_index}>"