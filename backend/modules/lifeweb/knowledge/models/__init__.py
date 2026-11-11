"""知识库（V1.3）的 ORM 模型。

三个模型都在这里 re-export：`modules/registry.py` 只需要一行 import 就能把
整张图交给 Alembic。
"""

from modules.lifeweb.knowledge.models.async_task import AsyncTask
from modules.lifeweb.knowledge.models.document import Document
from modules.lifeweb.knowledge.models.document_chunk import DocumentChunk

__all__ = ["AsyncTask", "Document", "DocumentChunk"]