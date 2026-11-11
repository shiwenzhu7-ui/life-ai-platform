"""知识库业务编排 —— 上传、后台处理、删除。

这个模块回答一个问题：**一份文件从字节流变成「能被检索到」需要经过哪几步，
每一步失败了怎么收场。**

整条链路的顺序是硬约束（见下面的 `process_document`），核心是两条：

1. **先写 MySQL，再写 Milvus。**
   MySQL 是真相来源（source of truth）：正文、状态、块 id 都在它这里；
   Milvus 只是「id → 向量」的从属索引。反过来先写向量的话，
   一旦 MySQL 那一步失败，检索会命中一批查不到原文的 id ——
   用户看到空引用卡片，日志里却全是正常的，属于最难排查的一类 bug。
   反过来的失败（MySQL 有、Milvus 没有）只是「暂时搜不到」，数据是完整的，
   重试或重建索引都能修。

2. **`documents.status = 'ready'` 是整条链路的最后一步。**
   它代表「MySQL 写完了、Milvus 也写完了」。检索必须带上 `status='ready'` 这个条件，
   它同时充当 Milvus 的一致性开关：写到一半失败的文档，即使 Milvus 里残留了
   一些向量，也永远不会出现在检索结果里。

删除同样是三处：MySQL 行（硬删）、磁盘文件、Milvus 向量。
前两处同步做完，Milvus 那一步失败只记日志不阻塞 —— 残留的向量没有
`documents` 行可对应，检索时按 `status='ready'` 与文档存在性两道条件挡掉，
可以事后按 user_id/document_id 批量清理。
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import Any
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import embedding, vectorstore
from core.config import settings
from core.db import SessionLocal
from core.errors import AppError, not_found, validation_error
from core.time import now_utc
from modules.lifeweb.knowledge.chunker import count_characters, split_text
from modules.lifeweb.knowledge.models.async_task import AsyncTask
from modules.lifeweb.knowledge.models.document import DOC_EXTENSIONS, Document
from modules.lifeweb.knowledge.models.document_chunk import DocumentChunk
from modules.lifeweb.knowledge.parser import (
    ParseError,
    guess_ext,
    parse_bytes,
    sanitize_filename,
)

logger = logging.getLogger(__name__)

# 读上传流时每次读多少。64KB 是常见的折中：太小系统调用多，太大占内存
_READ_CHUNK = 64 * 1024

# 写入 Milvus 时每批多少条。一次 insert 太多会让单条 gRPC 消息过大，
# 500 条足够摊薄往返开销，又不会撞到消息体积上限
_INSERT_BATCH = 500

# 存盘的根目录（相对 backend/）。与 stored_path 的定义一致（见 models/document.py）
UPLOAD_ROOT = Path("uploads")


def stored_file(doc: Document) -> Path:
    """文档对应的磁盘路径 = 上传根目录 + stored_path。

    所有读写磁盘的地方都必须走这里。不要用 `UPLOAD_ROOT / str(user_id) / name`
    自己拼：stored_path 已经带了 `<user_id>/` 这一层，再拼一次就成了
    `uploads/1/1/xxx.pdf` —— 测试因为覆盖了 UPLOAD_ROOT 才会暴露这个错，
    生产环境写入和读取一起错、反而看不见。
    """
    return UPLOAD_ROOT / doc.stored_path


def _storage_error(message: str) -> AppError:
    """存储相关错误统一用 503。

    用 503 而不是 500：这几乎总是「Milvus 虚拟机没开」这类依赖不可用，
    不是代码 bug，前端可以提示「稍后重试」而不是「服务器坏了」。
    """
    return AppError("STORAGE_UNAVAILABLE", message, 503)


# ---------------------------------------------------------------- 读上传


def read_upload(file: UploadFile) -> tuple[bytes, str, str]:
    """读完整条上传流，返回 (字节, 净化后的文件名, sha256)。

    两种拒绝要在这里就发生，而不是等读完再看长度：
      - 扩展名不对 → 422，一个字节都不用读
      - 超过上限    → 413，读到超限立刻停，绝不把 20MB 全收进内存再判断

    Content-Length 头**不可信**（客户端能伪造），所以上限只在真实读取的字节数上判断。
    sha256 边读边算，避免读完再遍历一遍 10MB 的 bytes。
    """
    filename = sanitize_filename(file.filename or "")
    ext = guess_ext(filename)

    if ext not in DOC_EXTENSIONS:
        raise validation_error(
            f"不支持的文件类型：{ext or '（无扩展名）'}，只支持 {' / '.join(DOC_EXTENSIONS)}"
        )

    buf = bytearray()
    digest = hashlib.sha256()
    limit = settings.kb_max_upload_bytes

    while True:
        chunk = file.file.read(_READ_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise AppError(
                "PAYLOAD_TOO_LARGE",
                f"文件超过 {limit // (1024 * 1024)}MB 上限",
                413,
            )
        digest.update(chunk)

    if not buf:
        raise validation_error("文件是空的")

    return bytes(buf), filename, digest.hexdigest()


# ---------------------------------------------------------------- 上传


def create_document(db: Session, user_id: int, data: bytes, filename: str, sha256: str) -> Document:
    """去重 + 落盘 + 建记录。**不含**后台处理（那一步由 router 调度）。

    去重先查一次是为了给用户一个友好的 409（带 existing_id / 当前状态），
    但真正防止并发重复的是数据库上的 `uk_documents_user_sha`：
    两个标签页同时上传同一份文件时，双方都能通过「先查」，只有一个能插进去，
    另一个触发 IntegrityError —— 这里捕获它并转成同样的 409。
    「先查再插」中间永远有时间窗口，靠唯一索引兜底才是对的。
    """
    existing = db.scalar(
        select(Document).where(
            Document.user_id == user_id,
            Document.sha256 == sha256,
        )
    )
    if existing is not None:
        raise _duplicate(existing)

    ext = guess_ext(filename)
    # 磁盘名用随机 uuid，不用用户给的文件名：
    # 防路径穿越、防中文名在 Windows 上的编码坑、防同名覆盖
    stored_name = f"{uuid.uuid4().hex}.{ext}"

    abs_path = UPLOAD_ROOT / str(user_id) / stored_name
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(data)
    except OSError as exc:
        logger.exception("写入上传文件失败：%s", abs_path)
        raise _storage_error("文件保存失败，请稍后重试") from exc

    # stored_path 只存相对 uploads/ 的 `<user_id>/<uuid>.<ext>`，**不带根目录**。
    # 根目录由 service.UPLOAD_ROOT 决定：生产是 backend/uploads，
    # 测试用 tmp_path 覆盖，这样同一份数据在两处都能正确落位。
    rel_path = f"{user_id}/{stored_name}"

    doc = Document(
        user_id=user_id,
        visibility="private",
        filename=filename,
        stored_path=rel_path,
        file_ext=ext,
        file_size=len(data),
        sha256=sha256,
        status="pending",
    )
    db.add(doc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # 并发重复：对方已经插进去了。磁盘上这份是多余的，删掉；
        # 注意不能删对方那份（文件名不同，安全）
        _unlink_quietly(abs_path)
        raise _duplicate_from_db(db, user_id, sha256) from exc

    db.refresh(doc)
    logger.info("文档已入库：id=%s user=%s file=%r size=%s", doc.id, user_id, filename, len(data))
    return doc


def _duplicate(existing: Document) -> AppError:
    return AppError(
        "DOCUMENT_DUPLICATE",
        "这份文件已经上传过",
        409,
        detail={"document_id": existing.id, "status": existing.status},
    )


def _duplicate_from_db(db: Session, user_id: int, sha256: str) -> AppError:
    existing = db.scalar(
        select(Document).where(
            Document.user_id == user_id,
            Document.sha256 == sha256,
        )
    )
    if existing is None:
        # 唯一索引冲突了却查不到 —— 只可能是别的用户撞上了（唯一键含 user_id，理论上不会）
        return AppError("DOCUMENT_DUPLICATE", "这份文件已经上传过", 409)
    return _duplicate(existing)


# ---------------------------------------------------------------- 后台处理


def add_task(db: Session, user_id: int, document_id: int, task_type: str = "doc_parse") -> AsyncTask:
    """建一条后台任务记录（queued）。"""
    task = AsyncTask(
        user_id=user_id,
        task_type=task_type,
        ref_id=document_id,
        status="queued",
        progress=0,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def process_document(document_id: int) -> None:
    """后台任务入口：解析 → 切块 → 嵌向量 → 写 Milvus。

    只收一个参数：`BackgroundTasks.add_task` 的参数会被序列化进任务队列，
    传 ORM 对象或 Session 都不行。任务记录在这里自己建（见 `add_task`），
    所以调用方只需要知道文档 id。

    由 FastAPI `BackgroundTasks` 在响应发出之后调用，所以：
      - **必须自己开会话**：请求级 Session 在响应发出时已经快关了，不能再借
      - **绝不向上抛**：这时已经没有任何人在等结果，抛出去只会变成一条
        「Exception in ASGI application」日志；状态要落到库里，让前端轮询看到

    失败时保证一件事：**`status='failed'` 的文档，`document_chunks` 里一定没有它的行。**
    否则用户重试时会新旧块混在一起，检索出现重复内容，而且没人分得清哪批是对的。
    """
    with SessionLocal() as db:
        doc = db.get(Document, document_id)
        if doc is None:
            logger.warning("后台任务找不到文档 %s，跳过", document_id)
            return

        try:
            task = add_task(db, doc.user_id, document_id)
            _mark_task(db, task, status="running", started=True)
            _run_pipeline(db, doc, task)
        except ParseError as exc:
            _fail(db, doc, task, str(exc))
        except AppError as exc:
            # _run_pipeline 里主动抛的业务错误（存储不可用等），消息已经能给人看
            _fail(db, doc, task, exc.message)
        except Exception:  # noqa: BLE001 —— 后台任务必须兜住一切，否则状态会永远卡住
            logger.exception("文档 %s 处理失败", document_id)
            _fail(db, doc, task, "处理失败，请稍后重试")


def _run_pipeline(db: Session, doc: Document, task: AsyncTask | None) -> None:
    """真正的处理步骤。成功返回，失败抛异常，由调用方统一收场。"""
    # ---- 0. 幂等清理：上一次失败留下的半截块，先清掉 ----
    # 这一步是重试能工作的前提。不做的话，失败重试会撞上
    # uk_chunks_doc_index（document_id + chunk_index）直接重复键报错
    _purge_chunks(db, doc)
    _purge_vectors(doc.id)

    # ---- 1. 解析 ----
    _set_status(db, doc, "parsing")
    _mark_task(db, task, progress=10, message="正在解析文件")

    path = UPLOAD_ROOT / doc.stored_path
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.exception("读取文件失败：%s", path)
        raise _storage_error("原文件丢失，请重新上传") from exc

    parsed = parse_bytes(data, doc.file_ext)

    # ---- 2. 切块 ----
    _set_status(db, doc, "chunking")
    _mark_task(db, task, progress=40, message="正在切分")

    pieces: list[tuple[int | None, str]] = []
    for page in parsed.pages:
        for text in split_text(
            page.text,
            size=settings.kb_chunk_size,
            overlap=settings.kb_chunk_overlap,
        ):
            pieces.append((page.page_no, text))

    if not pieces:
        # 解析成功但切完是空的（比如整份都是表格线、符号）
        raise ParseError("文件里没有可用的文字内容")

    # ---- 3. 写 MySQL（正文唯一落点） ----
    chunks = [
        DocumentChunk(
            document_id=doc.id,
            user_id=doc.user_id,
            chunk_index=index,
            page_no=page_no,
            content=text,
            token_count=count_characters(text),
        )
        for index, (page_no, text) in enumerate(pieces)
    ]
    db.add_all(chunks)
    db.commit()
    for chunk in chunks:
        db.refresh(chunk)  # 拿到自增 id —— 它同时是 Milvus 的主键

    # ---- 4. 嵌入 ----
    _set_status(db, doc, "embedding")
    _mark_task(db, task, progress=60, message=f"正在生成向量（{len(chunks)} 块）")

    try:
        vectors = embedding.embed_texts([c.content for c in chunks])
    except Exception as exc:  # noqa: BLE001 —— 模型加载失败、OOM 都在这里
        logger.exception("嵌入失败：文档 %s", doc.id)
        raise _storage_error("向量模型不可用，请稍后重试") from exc

    if len(vectors) != len(chunks):
        raise _storage_error("向量生成数量与分块数量不一致，请重试")

    # ---- 5. 写 Milvus（从属索引，最后一步才写） ----
    _mark_task(db, task, progress=90, message="正在写入向量库")
    _write_vectors(doc, chunks, vectors)

    # ---- 6. 收尾：ready 是「两边都写完了」的唯一标志 ----
    doc.chunk_count = len(chunks)
    doc.error_message = None
    _set_status(db, doc, "ready")
    _mark_task(db, task, status="success", progress=100, message=f"完成，共 {len(chunks)} 块", finished=True)
    logger.info("文档处理完成：id=%s 页=%s 块=%s", doc.id, len(parsed.pages), len(chunks))


def _write_vectors(doc: Document, chunks: list[DocumentChunk], vectors: list[list[float]]) -> None:
    """把 (chunk.id, vector, user_id, document_id) 批量写进 Milvus。

    写失败时按 document_id 反向删一次：Milvus 里的 document_id 字段就是为这个准备的。
    反删本身再失败就只记日志 —— 残留向量被 `status='ready'` 挡在检索之外，不会泄露数据。
    """
    try:
        vectorstore.ensure_collection()
        client = vectorstore.get_client()
        rows = [
            {
                "id": chunk.id,
                "vector": vector,
                "user_id": doc.user_id,
                "document_id": doc.id,
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        for start in range(0, len(rows), _INSERT_BATCH):
            client.insert(collection_name=settings.vector_collection, data=rows[start : start + _INSERT_BATCH])
    except Exception as exc:  # noqa: BLE001
        logger.exception("写入 Milvus 失败：文档 %s", doc.id)
        _purge_vectors(doc.id)
        raise _storage_error("向量库写入失败，请稍后重试") from exc


# ---------------------------------------------------------------- 状态维护


def _set_status(db: Session, doc: Document, status: str) -> None:
    doc.status = status
    db.commit()


def _mark_task(
    db: Session,
    task: AsyncTask | None,
    *,
    status: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    started: bool = False,
    finished: bool = False,
) -> None:
    if task is None:
        return
    if status is not None:
        task.status = status
    if progress is not None:
        task.progress = progress
    if message is not None:
        task.message = message[:255]
    if started:
        task.started_at = now_utc()
        task.attempts = (task.attempts or 0) + 1
    if finished:
        task.finished_at = now_utc()
    db.commit()


def _fail(db: Session, doc: Document, task: AsyncTask | None, message: str) -> None:
    """统一收场：标 failed + 写人话原因 + 清掉半截数据。"""
    db.rollback()  # 上一次没提交的改动不要带进这次
    try:
        _purge_chunks(db, doc)
        _purge_vectors(doc.id)
        doc.status = "failed"
        doc.chunk_count = 0
        doc.error_message = (message or "处理失败")[:500]
        db.commit()
        _mark_task(db, task, status="failed", message=doc.error_message, finished=True)
    except Exception:  # noqa: BLE001 —— 收场本身再失败，只能记日志
        logger.exception("标记文档 %s 失败时又出错", doc.id)
        db.rollback()


def _purge_chunks(db: Session, doc: Document) -> None:
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    db.commit()


def _purge_vectors(document_id: int) -> None:
    """按 document_id 从 Milvus 删掉该文档的所有向量。连不上只记日志。"""
    try:
        client = vectorstore.get_client()
        if not client.has_collection(settings.vector_collection):
            return
        client.delete(collection_name=settings.vector_collection, filter=f"document_id == {document_id}")
    except Exception:  # noqa: BLE001 —— 清理是尽力而为，不阻塞主流程
        logger.warning("清理 Milvus 向量失败：document_id=%s", document_id, exc_info=True)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("删除文件失败：%s", path, exc_info=True)


# ---------------------------------------------------------------- 查询


def list_documents(
    db: Session,
    user_id: int,
    *,
    page: int,
    size: int,
    status: str | None = None,
    keyword: str | None = None,
) -> tuple[list[Document], int]:
    """我的文档列表，按创建时间倒序。"""
    conditions = [Document.user_id == user_id]
    if status:
        conditions.append(Document.status == status)
    if keyword:
        conditions.append(Document.filename.like(f"%{keyword}%"))

    total = db.scalar(select(func.count()).select_from(Document).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(Document)
            .where(*conditions)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    )
    return items, total


def get_document(db: Session, user_id: int, document_id: int) -> Document:
    """取一份文档，别人的、已删的一律 404 —— 不泄露「这个 id 存在」。

    删除是硬删，行没了这里自然查不到，不需要额外的「是否已删」判断。

    `populate_existing()` 仍然保留：同一个会话里可能已经加载过这条文档的旧对象
    （比如删除接口先 get 再删），再查一次时要拿到库里的真实状态，
    而不是 identity map 里那个内存对象。
    """
    row = db.scalar(
        select(Document)
        .where(Document.id == document_id, Document.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise not_found("文档不存在")
    return row


def list_chunks(db: Session, document_id: int, *, limit: int = 50) -> list[DocumentChunk]:
    return list(
        db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
            .limit(limit)
        )
    )


# ---------------------------------------------------------------- 检索：Milvus 那一半

# 写完到可查之间要等多久。**这不是保守估计，是实测出来的数。**
# 真 Milvus 上 insert 之后立刻 get/query/search，三者一起返回空；等 1 秒才有。
# 默认一致性 Bounded 只承诺「秒级」可见，不是「同步」可见。
_FRESH_INSERT_WAIT_S = 0.8


def _search_rows(client: Any, vector: list[float], *, user_id: int, limit: int) -> list[dict]:
    """向 Milvus 要最近邻，返回 `[{"id", "distance", "entity": {...}}]`。

    这里有两道针对「刚写完查不到」的防线，都是真机上踩出来的（内存替身测不出）：

    1. **consistency_level="Strong"**：默认的 Bounded 只承诺「秒级可见」。
       显式要 Strong，让服务端按时间戳保证读到自己已提交的写入。
    2. **空结果等一下再问一次**：实测在 v2.6.25 上，insert 之后 0 秒的
       get / query / search 三者一齐返回空，等 1 秒后都有数据 —— 也就是说
       段从写入到进内存索引本身就有个短窗口，Strong 也挡不住（它保的是
       读时间戳，不是「段已建索引」）。所以第一次空了就等 `_FRESH_INSERT_WAIT_S`
       再问一次：真没数据时多花 0.8 秒，刚写完时正是救回来的那一次。

    为什么不用 search_iterator：它同样受段可见性约束，实测同样返回空，
    也没有实现 `__iter__`（只能手动 `next()`），多一条 API 差异不划算。
    """
    def _run() -> list[dict]:
        hits = client.search(
            collection_name=settings.vector_collection,
            data=[vector],
            limit=limit,
            filter=f"user_id == {user_id}",
            output_fields=["document_id"],
            consistency_level="Strong",
        )
        rows: list[dict] = []
        for batch in hits:
            rows.extend(row for row in batch if isinstance(row, dict))
        return rows

    first = _run()
    if first:
        return first

    # 第一次就是空的：可能是真没有，也可能是刚写进去还不知道。等一下再问一次。
    # 只在「空」的时候多花这一秒 —— 命中了的常见情况零额外延迟
    time.sleep(_FRESH_INSERT_WAIT_S)
    return _run()


# ---------------------------------------------------------------- 检索


def search_chunks(
    db: Session,
    user_id: int,
    query: str,
    *,
    top_k: int | None = None,
) -> list[dict]:
    """语义检索：把问题嵌入成向量，去 Milvus 找最近的几块，再回 MySQL 取正文。

    返回的每一块形如：

        {"document_id": 7, "chunk_id": 331, "filename": "预算.pdf",
         "page_no": 3, "chunk_index": 12, "content": "房贷 4200 ...",
         "score": 0.83}

    三个决定性的点，少一个都会出问题：

    1. **`filter=f"user_id == {user_id}"` 是安全边界，不是性能优化。**
       Milvus 自己不认「我的文档」这回事，它只会算距离。不带这个过滤，
       别人的文档片段会顺着相似度混进回答里 —— 这是整条 RAG 链路
       最容易写漏、又最难在测试里被发现的一行（测试得用两个用户才暴露）。

    2. **正文回 MySQL 取，不从 Milvus 取。** Milvus 里只存 id/vector/user_id/
       document_id 四个字段，正文本来就不在里面（见 `_write_vectors`）。
       把正文也塞进 Milvus 看似省一次查询，代价是同一段文字存两份、删改要同步两处。

    3. **只认 `status='ready'` 的文档。** 处理到一半的文档可能只写进了一部分切块，
       它的向量已经在 Milvus 里了（向量是分批写的），但正文还不全。
       让它进结果，用户会得到一个半截的答案。

    检索本身出错（Milvus 连不上、模型加载失败）**不抛异常**，返回空列表 ——
    知识库是「锦上添花」的能力，它挂掉不该让整轮对话失败。
    """
    text = (query or "").strip()
    if not text:
        return []

    limit = top_k or settings.kb_top_k
    try:
        vector = embedding.embed_query(text)
        client = vectorstore.get_client()
        if not client.has_collection(settings.vector_collection):
            return []
        rows = _search_rows(client, vector, user_id=user_id, limit=limit)
    except Exception:  # noqa: BLE001 —— 检索不可用时降级成「没检索到」，不阻断对话
        logger.warning("知识库检索失败：user=%s", user_id, exc_info=True)
        return []

    if not rows:
        return []

    scored: list[tuple[int, float]] = []
    for row in rows:
        entity = row.get("entity") or {}
        document_id = entity.get("document_id")
        if document_id is None:
            continue
        scored.append((int(document_id), float(row.get("distance") or 0.0)))
    if not scored:
        return []

    # 只放行 ready 的文档；status 一起取回来，省一次查库
    doc_ids = {doc_id for doc_id, _ in scored}
    ready_docs = {
        doc_id
        for doc_id, in db.execute(
            select(Document.id).where(
                Document.id.in_(doc_ids),
                Document.user_id == user_id,
                Document.status == "ready",
            )
        )
    }
    if not ready_docs:
        return []

    chunk_ids = [int(row.get("id")) for row in rows if row.get("id") is not None]
    details = db.execute(
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            DocumentChunk.chunk_index,
            DocumentChunk.page_no,
            DocumentChunk.content,
            Document.filename,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.id.in_(chunk_ids),
            DocumentChunk.document_id.in_(ready_docs),
            Document.user_id == user_id,
        )
    ).all()
    by_chunk = {row.id: row for row in details}

    # 按 Milvus 给的相似度从高到低走；同一份文档只留最相似的那一块，
    # 免得一个问题的答案被同一份文件刷屏。跨文档时保留多个来源，
    # 这正是 RAG 相对「直接塞全文」的价值所在
    seen_docs: set[int] = set()
    results: list[dict] = []
    for doc_id, score in scored:
        if doc_id not in ready_docs or doc_id in seen_docs:
            continue
        for chunk_id in chunk_ids:
            row = by_chunk.get(chunk_id)
            if row is None or row.document_id != doc_id:
                continue
            seen_docs.add(doc_id)
            results.append(
                {
                    "document_id": row.document_id,
                    "chunk_id": row.id,
                    "filename": row.filename,
                    "page_no": row.page_no,
                    "chunk_index": row.chunk_index,
                    "content": row.content,
                    "score": round(score, 4),
                }
            )
            break
    return results[:limit]


# ---------------------------------------------------------------- 删除


def delete_document(db: Session, user_id: int, document_id: int) -> None:
    """硬删文档：MySQL 行、块、磁盘文件同步处理，Milvus 尽力而为。

    顺序是有讲究的：
      1. 先删 documents 行 —— 这一步成功之后，列表和检索立刻看不到它了
      2. 再删 chunks 和磁盘文件 —— 每一步失败都只能在日志里留痕，
         因为「已经删了」这件事不能再回滚给用户
      3. 最后删 Milvus —— 最可能失败的一步放最后，它失败了也不影响前两步的结果

    `document_chunks.document_id` 没有外键级联（那是另一套取舍），
    所以 chunks 必须显式删，顺序放在 documents 之后也行、之前也行 ——
    放之后是为了让「文档已不可见」这个信号先落地。
    """
    doc = get_document(db, user_id, document_id)

    db.delete(doc)
    db.commit()

    _purge_chunks(db, doc)
    _purge_vectors(doc.id)
    _unlink_quietly(UPLOAD_ROOT / doc.stored_path)

    logger.info("文档已删除：id=%s user=%s", doc.id, user_id)