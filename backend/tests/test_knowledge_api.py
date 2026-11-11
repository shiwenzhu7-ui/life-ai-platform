"""知识库接口测试（V1.3 第 3 步：上传 → 后台处理 → 列表/详情/删除）。

三条纪律：

1. **不碰真实的 Milvus 和嵌入模型。**
   `vectorstore.get_client()` 与 `embedding.embed_texts()` 全部被 monkeypatch 成
   内存替身（见 `_patch_backend`）。真实 Milvus 在虚拟机里，跑测试时不一定开着；
   真实嵌入模型要加载 90MB 的 ONNX，每个用例都加载一遍会让测试跑到几分钟。
   替身做法与 `tests/test_llm_service.py` 里 `FakeProvider` 一致。

2. **测试文件写进临时目录。**
   `service.UPLOAD_ROOT` 指向 pytest 的 `tmp_path`，跑完不留垃圾，
   也不会往仓库里写 `backend/uploads/`。

3. **后台任务在这里是同步执行的。**
   `TestClient` 在响应返回后才跑 `BackgroundTasks`，但 `with TestClient(...) as c`
   的上下文管理器会在退出时等它跑完，所以 `client.post(...)` 之后再查库，
   看到的一定是处理完之后的状态。想测「刚上传、还没处理」的中间态，
   直接把 `process_document` patch 成空函数。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from sqlalchemy import select

from core.deps import get_current_user_id
from modules.lifeweb.knowledge import service
from modules.lifeweb.knowledge.models.async_task import AsyncTask
from modules.lifeweb.knowledge.models.document import Document
from modules.lifeweb.knowledge.models.document_chunk import DocumentChunk

API = "/api/v1/knowledge/documents"

# 一段足够长、能切出多块的中文正文（每句 20 字左右，切分器会按句号断）
LONG_TEXT = (
    "今天是十一月十一日，天气晴朗。"
    "我早上八点起床，先喝了一杯温水。"
    "然后去楼下买了两个包子和一杯豆浆。"
    "回来之后开始整理上周的会议记录。"
    "会议纪要里提到下个月要上线知识库功能。"
    "这个功能需要把用户上传的文档切块并生成向量。"
    "最后统一存进向量数据库，方便语义检索。"
) * 4


class FakeVectorStore:
    """内存版的 Milvus。

    只实现 service 用到的三个方法：ensure_collection / get_client / insert / delete / has_collection。
    故意用 dict 而不是 list：重复写同一个 id 是覆盖而不是追加，
    这样「幂等清理没做干净」的 bug 会暴露成「条数对不上」。
    """

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.fail_insert = False
        self.fail_delete = False
        self.insert_calls = 0

    # ---- 伪装成 client ----
    def has_collection(self, name: str) -> bool:  # noqa: ARG002 —— 签名对齐 pymilvus
        return True

    def insert(self, collection_name: str, data: list[dict]) -> None:  # noqa: ARG002
        self.insert_calls += 1
        if self.fail_insert:
            raise RuntimeError("milvus down")
        for row in data:
            self.rows[int(row["id"])] = row

    def delete(self, collection_name: str, filter: str) -> None:  # noqa: ARG002
        if self.fail_delete:
            raise RuntimeError("milvus down")
        # 只支持 service 里用到的这一种：document_id == N
        _, _, raw = filter.partition("==")
        doc_id = int(raw.strip())
        for rid, row in list(self.rows.items()):
            if int(row["document_id"]) == doc_id:
                del self.rows[rid]

    def ensure_collection(self) -> None:
        pass

    def get_client(self) -> "FakeVectorStore":
        return self


@pytest.fixture()
def fake_store(monkeypatch, tmp_path) -> FakeVectorStore:
    """把向量库、嵌入和上传目录全部换成测试替身。"""
    store = FakeVectorStore()
    monkeypatch.setattr(service.vectorstore, "get_client", store.get_client)
    monkeypatch.setattr(service.vectorstore, "ensure_collection", store.ensure_collection)
    # 512 维无所谓，只要维度一致；用 8 维省内存
    monkeypatch.setattr(
        service.embedding,
        "embed_texts",
        lambda texts: [[0.1] * 8 for _ in texts],
    )
    monkeypatch.setattr(service, "UPLOAD_ROOT", tmp_path / "uploads")
    return store


def _upload(client, content: bytes, filename: str = "笔记.txt", ext_hint: str = "text/plain"):
    return client.post(
        API,
        files={"file": (filename, io.BytesIO(content), ext_hint)},
    )


def _doc(db, doc_id: int) -> Document:
    """另开会话按 id 读一份文档；删除之后应该返回 None（硬删，行没了）。

    另开会话是为了避开 REPEATABLE READ 的旧快照。
    """
    from tests.conftest import new_session

    with new_session() as s:
        return s.get(Document, doc_id)


def _chunk_count(db, doc_id: int) -> int:
    from tests.conftest import new_session

    with new_session() as s:
        return len(list(s.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc_id))))


# ---------------------------------------------------------------- 上传成功


def test_upload_txt_processes_to_ready(client, fake_store):
    """正常路径：202 → 后台跑完 → ready，切块进 MySQL、向量进 Milvus。"""
    content = LONG_TEXT.encode("utf-8")
    resp = _upload(client, content, "周报.txt")

    assert resp.status_code == 202
    body = resp.json()
    # 响应是在后台处理之前构造的，所以这里是 pending —— 正好验证「立刻返回」
    assert body["status"] == "pending"
    assert body["filename"] == "周报.txt"
    assert body["file_ext"] == "txt"
    assert body["file_size"] == len(content)

    doc = _doc(None, body["id"])
    assert doc.status == "ready"
    assert doc.chunk_count > 0
    assert doc.error_message is None

    chunks = _chunk_count(None, doc.id)
    assert chunks == doc.chunk_count
    # Milvus 里的向量数量与 MySQL 块数一致，且主键一一对应
    assert len(fake_store.rows) == doc.chunk_count

    from tests.conftest import new_session

    with new_session() as s:
        ids = {c.id for c in s.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc.id))}
    assert ids == set(fake_store.rows)


def test_upload_creates_async_task_record(client, fake_store):
    """每份文档留一条 async_tasks 账：用于事后查「这次处理花了多久、为什么失败」。"""
    resp = _upload(client, LONG_TEXT.encode("utf-8"))
    doc_id = resp.json()["id"]

    from tests.conftest import new_session

    with new_session() as s:
        tasks = list(s.scalars(select(AsyncTask).where(AsyncTask.ref_id == doc_id)))
    assert len(tasks) == 1
    assert tasks[0].task_type == "doc_parse"
    assert tasks[0].status == "success"
    assert tasks[0].progress == 100
    assert tasks[0].attempts == 1
    assert tasks[0].started_at is not None
    assert tasks[0].finished_at is not None


def test_upload_stores_file_on_disk(client, fake_store, tmp_path):
    """原文件落盘：路径是 uploads/<user_id>/<uuid>.<ext>，不是用户给的名字。"""
    resp = _upload(client, LONG_TEXT.encode("utf-8"), "我的 笔记.txt")
    doc = _doc(None, resp.json()["id"])

    path = Path(service.UPLOAD_ROOT) / doc.stored_path
    assert path.exists()
    # 用 uuid 命名，原始文件名不会出现在磁盘路径里
    assert "笔记" not in doc.stored_path
    assert doc.stored_path.startswith("1/")
    assert doc.stored_path.endswith(".txt")


def test_upload_docx(client, fake_store):
    """DOCX 走另一条解析分支：没有页码，page_no 全是 null。"""
    from docx import Document as Docx

    buf = io.BytesIO()
    d = Docx()
    for line in ["第一段：项目排期", "第二段：风险点"] + [LONG_TEXT] * 2:
        d.add_paragraph(line)
    d.save(buf)

    resp = _upload(client, buf.getvalue(), "方案.docx")
    assert resp.status_code == 202

    doc = _doc(None, resp.json()["id"])
    assert doc.status == "ready"
    assert doc.chunk_count > 0

    from tests.conftest import new_session

    with new_session() as s:
        pages = {c.page_no for c in s.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc.id))}
    assert pages == {None}


# ---------------------------------------------------------------- 上传失败 / 拒绝


def test_upload_empty_file_is_422(client, fake_store):
    resp = _upload(client, b"")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("name", ["a.exe", "b.md", "无扩展名", "c.pdf.md"])
def test_upload_bad_extension_is_422(client, fake_store, name):
    resp = _upload(client, b"hello", name)
    assert resp.status_code == 422
    assert "不支持的文件类型" in resp.json()["message"]


def test_upload_too_large_is_413(client, fake_store, monkeypatch):
    """超过上限立刻停，不会把超限数据全收进内存。"""
    monkeypatch.setattr(service.settings, "kb_max_upload_bytes", 1024)
    resp = _upload(client, b"x" * 2048)
    assert resp.status_code == 413
    assert resp.json()["code"] == "PAYLOAD_TOO_LARGE"


def test_upload_duplicate_is_409(client, fake_store):
    """同一份内容传两次 → 409，并告诉前端已有那份的 id。"""
    data = LONG_TEXT.encode("utf-8")
    first = _upload(client, data)
    assert first.status_code == 202

    second = _upload(client, data, "换个名字.txt")
    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "DOCUMENT_DUPLICATE"
    assert body["detail"]["document_id"] == first.json()["id"]
    assert body["detail"]["status"] == "ready"


def test_upload_scanned_pdf_fails_with_reason(client, fake_store):
    """扫描件 PDF（无文字层）→ 落到 failed，error_message 是人话，不留半截块。"""
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    resp = _upload(client, buf.getvalue(), "扫描件.pdf")
    assert resp.status_code == 202

    doc = _doc(None, resp.json()["id"])
    assert doc.status == "failed"
    assert "OCR" in doc.error_message or "没有可提取的文字" in doc.error_message
    # 关键：失败时不能留下任何块
    assert _chunk_count(None, doc.id) == 0
    assert doc.chunk_count == 0


def test_milvus_failure_rolls_back_chunks(client, fake_store):
    """Milvus 写失败 → failed，MySQL 里的块被清干净（不留「有正文没向量」的孤儿）。

    这是整条链路里最重要的一条约束：failed 的文档，document_chunks 必须为空。
    """
    fake_store.fail_insert = True

    resp = _upload(client, LONG_TEXT.encode("utf-8"))
    doc = _doc(None, resp.json()["id"])

    assert doc.status == "failed"
    assert "向量" in doc.error_message
    assert _chunk_count(None, doc.id) == 0
    assert fake_store.rows == {}

    from tests.conftest import new_session

    with new_session() as s:
        task = s.scalars(select(AsyncTask).where(AsyncTask.ref_id == doc.id)).one()
    assert task.status == "failed"


def test_retry_after_failure_does_not_duplicate_chunks(client, fake_store):
    """失败后重跑一遍（模拟补偿重试），块数不会翻倍。

    重试走的是 `_run_pipeline` 开头的幂等清理：先按 document_id 清空 chunks 和向量。
    不做这一步的话，第二次 INSERT 会撞 uk_chunks_doc_index 直接报错。
    """
    fake_store.fail_insert = True
    resp = _upload(client, LONG_TEXT.encode("utf-8"))
    doc_id = resp.json()["id"]
    assert _doc(None, doc_id).status == "failed"

    # Milvus 恢复，手动重跑后台任务（等价于补偿任务重入队）
    fake_store.fail_insert = False
    service.process_document(doc_id)

    doc = _doc(None, doc_id)
    assert doc.status == "ready"
    assert _chunk_count(None, doc_id) == doc.chunk_count
    assert len(fake_store.rows) == doc.chunk_count

    from tests.conftest import new_session

    with new_session() as s:
        # 两次尝试各记一条账，attempts 都是 1（每次任务记录自己的尝试次数）
        tasks = list(s.scalars(select(AsyncTask).where(AsyncTask.ref_id == doc_id)))
    assert len(tasks) == 2
    assert {t.status for t in tasks} == {"failed", "success"}


def test_deleted_file_fails_gracefully(client, fake_store):
    """磁盘文件被外部删掉 → failed + 人话提示，而不是 500 或卡在 parsing。"""
    resp = _upload(client, LONG_TEXT.encode("utf-8"))
    doc_id = resp.json()["id"]

    # 先让文档变成 pending，再把文件删掉，然后手动重跑
    doc = _doc(None, doc_id)
    (Path(service.UPLOAD_ROOT) / doc.stored_path).unlink()
    fake_store.rows.clear()

    service.process_document(doc_id)

    doc = _doc(None, doc_id)
    assert doc.status == "failed"
    assert "原文件丢失" in doc.error_message


# ---------------------------------------------------------------- 列表


def test_list_documents_only_mine(client, fake_store):
    _upload(client, LONG_TEXT.encode("utf-8"), "我的.txt")

    # 切成 2 号用户，看不到 1 号的
    from app import app

    app.dependency_overrides[get_current_user_id] = lambda: 2
    resp = client.get(API)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_list_documents_filter_and_paginate(client, fake_store):
    _upload(client, LONG_TEXT.encode("utf-8"), "甲.txt")
    _upload(client, (LONG_TEXT + "甲").encode("utf-8"), "乙.txt")
    _upload(client, b"a short text without punctuation", "丙.txt")

    resp = client.get(API, params={"size": 2})
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["pages"] == 2
    # 倒序：后传的在前
    assert body["items"][0]["filename"] == "丙.txt"

    by_kw = client.get(API, params={"keyword": "乙"}).json()
    assert by_kw["total"] == 1
    assert by_kw["items"][0]["filename"] == "乙.txt"

    by_status = client.get(API, params={"status": "ready"}).json()
    assert by_status["total"] == 3
    failed = client.get(API, params={"status": "failed"}).json()
    assert failed["total"] == 0


def test_list_documents_hides_internal_fields(client, fake_store):
    """sha256 和 stored_path 不能出现在响应里 —— 前者是内部指纹，后者暴露磁盘结构。"""
    _upload(client, LONG_TEXT.encode("utf-8"))
    item = client.get(API).json()["items"][0]
    assert "sha256" not in item
    assert "stored_path" not in item


# ---------------------------------------------------------------- 详情


def test_get_document_returns_chunks(client, fake_store):
    resp = _upload(client, LONG_TEXT.encode("utf-8"))
    doc_id = resp.json()["id"]

    detail = client.get(f"{API}/{doc_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["document"]["status"] == "ready"
    assert len(body["chunks"]) > 0
    # 切块按序号升序
    assert [c["chunk_index"] for c in body["chunks"]] == list(range(len(body["chunks"])))
    assert all(c["token_count"] > 0 for c in body["chunks"])


def test_get_document_chunk_limit_zero(client, fake_store):
    """轮询时只要状态，不要切块 —— chunk_limit=0 省掉一次查询。"""
    doc_id = _upload(client, LONG_TEXT.encode("utf-8")).json()["id"]
    body = client.get(f"{API}/{doc_id}", params={"chunk_limit": 0}).json()
    assert body["document"]["status"] == "ready"
    assert body["chunks"] == []


def test_get_document_other_user_is_404(client, fake_store):
    doc_id = _upload(client, LONG_TEXT.encode("utf-8")).json()["id"]

    from app import app

    app.dependency_overrides[get_current_user_id] = lambda: 2
    resp = client.get(f"{API}/{doc_id}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 删除


def test_delete_document_cleans_everything(client, fake_store):
    """删除要三处干净：MySQL 行（硬删）、切块、Milvus 向量、磁盘文件。"""
    doc_id = _upload(client, LONG_TEXT.encode("utf-8")).json()["id"]
    doc = _doc(None, doc_id)
    path = Path(service.UPLOAD_ROOT) / doc.stored_path
    assert path.exists()
    assert fake_store.rows

    resp = client.delete(f"{API}/{doc_id}")
    assert resp.status_code == 204

    assert _doc(None, doc_id) is None  # 硬删后行不存在
    assert _chunk_count(None, doc_id) == 0
    assert fake_store.rows == {}
    assert not path.exists()


def test_delete_document_twice_is_404(client, fake_store):
    doc_id = _upload(client, LONG_TEXT.encode("utf-8")).json()["id"]
    assert client.delete(f"{API}/{doc_id}").status_code == 204
    assert client.delete(f"{API}/{doc_id}").status_code == 404


def test_delete_document_survives_milvus_down(client, fake_store):
    """Milvus 挂掉时删除接口照样 204：残留向量被 status 挡在检索之外。"""
    doc_id = _upload(client, LONG_TEXT.encode("utf-8")).json()["id"]
    fake_store.fail_delete = True

    assert client.delete(f"{API}/{doc_id}").status_code == 204
    assert _doc(None, doc_id) is None
    assert _chunk_count(None, doc_id) == 0


def test_reupload_after_delete_works(client, fake_store):
    """删掉之后再传同一个文件必须成功，不会被唯一索引挡住。

    唯一索引是 (user_id, sha256)。删除是硬删，行真的没了 ——
    所以唯一索引自然放行，不需要在去重查询里加任何「排除已删除」的条件。
    这条用例盯的就是这件事：如果哪天有人把删除改回软删，它会先红。
    """
    data = LONG_TEXT.encode("utf-8")
    first = _upload(client, data).json()["id"]
    assert client.delete(f"{API}/{first}").status_code == 204
    assert _doc(None, first) is None

    again = _upload(client, data)
    assert again.status_code == 202, again.text
    assert again.json()["id"] != first