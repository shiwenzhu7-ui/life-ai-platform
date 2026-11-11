"""知识库接口 —— 只做三件事，不含业务规则。

1. 绑定并校验 HTTP 参数
2. 注入 user_id（JWT 解析在 deps 层，本层看不到令牌）
3. 调 service，把 ORM 对象转成响应 Schema

路由前缀 `/knowledge`，下面的 `documents` 是资源名：
    POST   /knowledge/documents            上传（202，立刻返回，后台处理）
    GET    /knowledge/documents            我的文档列表
    GET    /knowledge/documents/{id}       详情（含前 50 个切块）
    DELETE /knowledge/documents/{id}       删除（软删 + 清向量 + 删磁盘文件）

为什么上传返回 202 而不是 201：201 表示「资源已创建且可用」，
而这里文档只是插了一行 `pending`，真正可检索要等后台跑完 ——
202 Accepted 才是准确的说法，前端拿到 202 就该开始轮询状态。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile, status
from sqlalchemy.orm import Session

from core.db import get_db
from core.deps import get_current_user_id
from core.time import to_beijing_iso
from modules.lifeweb.knowledge import service
from modules.lifeweb.knowledge.schema import (
    ChunkOut,
    DocStatus,
    DocumentDetailOut,
    DocumentOut,
)
from modules.shared.pagination import Page

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["知识库"])


@router.post(
    "/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档（异步处理）",
)
def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="pdf / docx / txt，单文件不超过 20MB"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> DocumentOut:
    """上传一份文档。

    返回 202 时文档状态是 `pending`，前端应轮询 `GET /knowledge/documents/{id}`。
    同一用户重复上传内容相同的文件返回 409，`detail.document_id` 指向已有那份。

    鉴权在读取文件之前完成（`get_current_user_id` 是依赖），
    没登录的人一个字节都不会被收进来。
    """
    data, filename, sha256 = service.read_upload(file)

    doc = service.create_document(db, user_id, data, filename, sha256)

    # 响应发出之后才执行，所以用户不会卡在转圈上等 30 秒。
    # 注意：进程重启会让排队中的任务丢失，documents.status 会停在 pending ——
    # 这是 BackgroundTasks 的固有代价，补偿重试属于后续步骤。
    # 只传 document_id：add_task 的参数要能被序列化，ORM 对象不行
    background_tasks.add_task(service.process_document, doc.id)

    return DocumentOut.from_model(doc, to_beijing_iso)


@router.get("/documents", response_model=Page[DocumentOut], summary="我的文档列表")
def list_documents(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    status: DocStatus | None = Query(default=None, description="按处理状态筛选"),
    keyword: str | None = Query(default=None, max_length=50, description="模糊匹配文件名"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Page[DocumentOut]:
    """按创建时间倒序。只看得到自己的文档。"""
    items, total = service.list_documents(
        db, user_id, page=page, size=size, status=status, keyword=keyword
    )
    return Page.create(
        [DocumentOut.from_model(doc, to_beijing_iso) for doc in items], total, page, size
    )


@router.get(
    "/documents/{document_id}",
    response_model=DocumentDetailOut,
    summary="文档详情（含切块预览）",
)
def get_document(
    document_id: int,
    chunk_limit: int = Query(default=50, ge=0, le=200, description="附带多少条切块，0 = 不带"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> DocumentDetailOut:
    """详情页 + 轮询接口。

    轮询时用 `chunk_limit=0` 省掉切块查询 —— 前端只看 `status` 有没有变成 ready。

    返回 `{document, chunks}` 而不是一个平铺对象：
    切块列表可能为空，包一层之后前端不用去判断「这个字段是列表还是单条」。
    """
    doc = service.get_document(db, user_id, document_id)
    chunks = service.list_chunks(db, doc.id, limit=chunk_limit) if chunk_limit else []
    return DocumentDetailOut(
        document=DocumentOut.from_model(doc, to_beijing_iso),
        chunks=[
            ChunkOut(
                id=c.id,
                chunk_index=c.chunk_index,
                page_no=c.page_no,
                content=c.content,
                token_count=c.token_count,
            )
            for c in chunks
        ],
    )


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除文档",
)
def delete_document(
    document_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> None:
    """软删文档，同时清掉切块、磁盘文件与 Milvus 向量。

    再删一次返回 404（已经查不到了），删别人的也是 404。
    """
    service.delete_document(db, user_id, document_id)