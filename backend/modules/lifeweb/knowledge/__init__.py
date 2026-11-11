"""知识库模块（V1.3）：文档上传、切块、向量化、检索。

    models/         documents / document_chunks / async_tasks 三张表
    parser.py       把 PDF/DOCX/TXT 读成带页码的纯文本段      ← 第 2 步
    chunker.py      按 512/64 切块                               ← 第 2 步
    service.py      编排：去重 → 存盘 → 建记录 → 后台处理        ← 第 3/4 步
    router.py       /knowledge 系列接口                          ← 第 3/4 步

MySQL 存正文（document_chunks.content），Milvus 只存向量 + 过滤字段，
两边的连接点就是 document_chunks.id（同时充当 Milvus 主键）。
"""

from modules.lifeweb.knowledge.models import AsyncTask, Document, DocumentChunk

__all__ = ["AsyncTask", "Document", "DocumentChunk"]