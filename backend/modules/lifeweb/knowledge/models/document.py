"""documents 表（知识库文档元信息）。

设计依据 `docs/数据库设计.md` §3.14，迁移 `d1knowledge`。

存的是「这份文件是什么、处理到哪一步了」，**不存正文**——
正文在 `document_chunks`，向量在 Milvus，两边靠 `document_chunks.id` 对上。

几处与设计文档的偏差（有意为之，代码里逐条注释）：

1. `user_id` 不加 `DEFAULT 0`，也不做「公共知识」：设计文档里 `0 = 公共知识（仅 admin 创建）`，
   但 V1.2 的管理后台一直没落地，`role='admin'` 连上传入口都没有。
   留一个永远写不进去的语义，比不留更难读。`visibility` 字段照建，V1.3 一律写 'private'。
2. 不加外键：与 tasks/expenses 的做法一致，存在性由应用层校验，
   外键会让「删用户」这种操作被文档表卡住。

状态机（`status`）：

    pending ──▶ parsing ──▶ chunking ──▶ embedding ──▶ ready
       └──────────── 任一步失败 ────────────▶ failed

中间三态是给前端轮询看进度的，不是给业务逻辑判断用的：
业务只关心 `ready`（能检索）和 `failed`（带 error_message 可重试）。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

DOC_STATUSES = ("pending", "parsing", "chunking", "embedding", "ready", "failed")
DOC_VISIBILITIES = ("private", "public")
# 支持的上传格式。加格式要同时改 knowledge/parser.py 的分发逻辑
DOC_EXTENSIONS = ("pdf", "docx", "txt")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        # 同一用户同一份文件只留一条：重复上传直接返回已有记录，
        # 不浪费一次解析 + N 次嵌入（嵌入是最贵的一步）
        UniqueConstraint("user_id", "sha256", name="uk_documents_user_sha"),
        # 「我的文档列表，按状态筛」——前端轮询和列表页都用它
        Index("idx_documents_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # V1.3 只有 private。不给 server_default：调用方必须显式表态，
    # 将来做公共知识时才会有人注意到这里需要判断权限
    visibility: Mapped[str] = mapped_column(
        Enum(*DOC_VISIBILITIES, name="doc_visibility"), nullable=False, server_default="private"
    )

    # 净化之后的文件名（去掉路径分隔符、控制字符，限长），仅用于展示
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 磁盘上的真实位置，相对 backend/uploads/ 的 `<user_id>/<uuid>.<ext>`。
    # 不存根目录：根目录由 service.UPLOAD_ROOT 决定（测试用 tmp_path 覆盖）。
    # 不用原始文件名存盘：同名文件会互相覆盖，中文名在 Windows 上还有编码坑
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(10), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        Enum(*DOC_STATUSES, name="doc_status"), nullable=False, server_default="pending"
    )
    # 成功写入 MySQL 且向量也进了 Milvus 的分块数。
    # 与 document_chunks 实际行数不一致，就说明有分块的向量没写成功 —— 那正是 failed 的判据
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 只给人看的短原因（如「PDF 加密，无法读取」），不带堆栈
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # 删除是硬删（DELETE），表里没有 deleted_at 列。
    # 删除分三处（MySQL 行、Milvus 向量、磁盘文件），后两处失败靠日志留痕，
    # 残留向量被 status 挡在检索之外（「安全垃圾」）

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Document id={self.id} filename={self.filename!r} status={self.status}>"