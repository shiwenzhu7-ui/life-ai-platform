"""知识库接口的出入参 Schema。

只描述 HTTP 边界上的数据形状，不放业务规则：
「这个文件的扩展名能不能收」「这个人是不是文件的主人」都在 router / service 里判断。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# 与 models/document.py 的 DOC_STATUSES 一一对应。
# 这里再写一遍而不是复用 ORM 常量，是因为 OpenAPI 文档里需要一个明确的枚举值列表，
# 而 ORM 里的 tuple 会被写成 `array of string`，前端拿不到可选项。
DocStatus = Literal["pending", "parsing", "chunking", "embedding", "ready", "failed"]


class DocumentOut(BaseModel):
    """文档列表 / 详情里的一条。

    刻意不含 `sha256`、`stored_path`：
    前者是内部去重指纹（用户不需要看到），后者暴露磁盘结构。
    """

    id: int
    filename: str
    file_ext: str
    file_size: int = Field(description="字节数")
    status: DocStatus
    chunk_count: int = Field(description="成功切出的块数，0 表示还没开始或失败")
    error_message: str | None = Field(default=None, description="失败时的简短原因，非失败为 null")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, doc, iso_datetime) -> "DocumentOut":
        """ORM → Schema。

        `iso_datetime` 是 core.time.to_beijing_iso —— 传进来而不是在这里 import，
        是为了让 Schema 层保持「零业务依赖」，测试里也好替换。
        """
        return cls(
            id=doc.id,
            filename=doc.filename,
            file_ext=doc.file_ext,
            file_size=doc.file_size,
            status=doc.status,
            chunk_count=doc.chunk_count,
            error_message=doc.error_message,
            created_at=iso_datetime(doc.created_at),
            updated_at=iso_datetime(doc.updated_at),
        )


class ChunkOut(BaseModel):
    """一个切片。检索结果与「文档详情里展开看切块」都用它。

    `page_no` 为 null 表示来源是 DOCX/TXT（这两类文件没有可靠页码）。
    """

    id: int
    chunk_index: int = Field(description="文档内序号，从 0 开始")
    page_no: int | None = Field(default=None, description="PDF 的页码，从 1 开始；无页码为 null")
    content: str
    token_count: int = Field(description="字符数（不含空白），不是分词数")


class DocumentDetailOut(BaseModel):
    """文档详情 = 元信息 + 切块预览。

    包一层而不是平铺：chunks 可能为空数组，独立字段让前端少一次类型判断。
    """

    document: DocumentOut
    chunks: list[ChunkOut]