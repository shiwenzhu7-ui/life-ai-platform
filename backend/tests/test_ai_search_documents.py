"""V1.3 第 4 步：把知识库接进 AI 对话（`search_personal_documents`）。

前三个测试文件证明的是「文件怎么进库」（解析、切分、入库）；
这一份证明「进库之后能不能**安全地**被用上」。要证明的七件事：

1. 检索结果真的回灌给了模型，模型据此说话，且响应里带 `references`
2. **跨用户检索引擎返回空** —— 这是需求 §7.7 的验收项，也是最容易写漏的安全边界。
   测试特意让假引擎**不认 filter**：如果代码哪天删掉了 `user_id == N`，
   这里会立刻失败，而不是等到线上才发现别人的文档混进了回答
3. 只有 `status='ready'` 的文档能被检索到；处理中的、失败的都不行
4. 检索命中**不产生待确认卡片**，业务表一行不动（它是查询，不是写入）
5. 查不到时回灌文案必须明确「没有」，逼模型承认而不是拿常识编
6. 用户在问资料、模型却没检索时补问一次（`_SEARCH_HINT`）
7. 普通聊天、查待办都不带 `references`，老响应一个字段都不多

全部用 FakeProvider + 内存版 Milvus，不联网、不加载真实模型。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import API, new_session

from app import app
from modules.lifeweb.ai.models import ChatMessage, PendingAction
from modules.lifeweb.ai.provider import FakeProvider, LLMReply
from modules.lifeweb.ai.router import get_provider
from modules.lifeweb.knowledge import service as knowledge_service
from modules.lifeweb.knowledge.models.document import Document
from modules.lifeweb.knowledge.models.document_chunk import DocumentChunk
from modules.lifeweb.tasks.model import Task


def _call(name: str = "search_personal_documents", **arguments) -> dict:
    return {
        "id": f"call_{name}",
        "type": "function",
        "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }


def _reply(content=None, *, calls=None, tokens=(10, 5)) -> LLMReply:
    return LLMReply(
        content=content, tool_calls=calls, token_in=tokens[0], token_out=tokens[1]
    )


class FakeVectorStore:
    """内存版 Milvus，**故意不实现 user_id 过滤**。

    真实的 Milvus 会老老实实按 `filter="user_id == N"` 筛；这里要是也筛一遍，
    「service 忘了写 filter」这个 bug 就会被测试替身盖住 —— 用户在测试里看不到，
    上线后看到的是别人的文档。所以替身只认「谁最相似」，隔离的责任**全部**压在
    `search_chunks` 自己的 SQL 与 filter 上。
    """

    def __init__(self) -> None:
        # chunk_id -> {"id", "vector", "user_id", "document_id"}
        self.rows: dict[int, dict] = {}
        self.search_filter: str | None = None
        self.consistency_level: str | None = None
        self.fail_search = False

    def has_collection(self, name: str) -> bool:  # noqa: ARG002
        return True

    def insert(self, collection_name: str, data: list[dict]) -> None:  # noqa: ARG002
        for row in data:
            self.rows[int(row["id"])] = row

    def delete(self, collection_name: str, filter: str) -> None:  # noqa: ARG002
        _, _, raw = filter.partition("==")
        doc_id = int(raw.strip())
        for rid, row in list(self.rows.items()):
            if int(row["document_id"]) == doc_id:
                del self.rows[rid]

    def search(
        self,
        collection_name: str,  # noqa: ARG002
        data: list[list[float]],
        limit: int,
        filter: str | None = None,  # noqa: A002 —— 与 pymilvus 的签名对齐
        output_fields: list[str] | None = None,  # noqa: ARG002
        consistency_level: str | None = None,
    ) -> list[list[dict]]:
        # 真 pymilvus 的 search 接受 consistency_level，替身也得收：少一个参数
        # 会让 service 传过去就 TypeError，而这类签名漂移在真机上根本不会报
        self.search_filter = filter
        self.consistency_level = consistency_level
        if self.fail_search:
            raise RuntimeError("milvus down")
        # 不按向量算相似度，按写入顺序返回：测试只关心「命中了谁」，不关心分数准不准
        hits = [
            {"id": rid, "distance": 0.9 - index * 0.05, "entity": {"document_id": row["document_id"]}}
            for index, (rid, row) in enumerate(list(self.rows.items())[:limit])
        ]
        return [hits]


@pytest.fixture()
def fake_store(monkeypatch) -> FakeVectorStore:
    store = FakeVectorStore()
    monkeypatch.setattr(knowledge_service.vectorstore, "get_client", lambda: store)
    monkeypatch.setattr(
        knowledge_service.embedding,
        "embed_query",
        lambda text: [0.1] * 8,
    )
    return store


@pytest.fixture()
def provider():
    fake = FakeProvider()
    app.dependency_overrides[get_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_provider, None)


def _chat(client, message: str = "我文件里写了什么"):
    return client.post(f"{API}/ai/chat", json={"message": message})


def _tool_contents(provider: FakeProvider, call_index: int = 1) -> str:
    return provider.calls[call_index]["messages"][-1]["content"]


def _seed_doc(
    *,
    user_id: int = 1,
    filename: str = "预算.xlsx",
    status: str = "ready",
    contents: tuple[str, ...] = ("这个月房贷 4200 元，每月五号扣款。",),
    page_nos: tuple[int | None, ...] | None = None,
) -> tuple[int, list[int]]:
    """直接造一份「已经处理完」的文档：documents + document_chunks 两处都写。

    不走上传接口：那边要跑解析、切分、嵌入一整条链，测检索隔离时这些噪音全无关。
    """
    pages = page_nos or tuple(None for _ in contents)
    with new_session() as s:
        doc = Document(
            user_id=user_id,
            filename=filename,
            file_ext="txt",
            file_size=100,
            stored_path=f"{user_id}/fake.txt",
            sha256=f"{user_id:016x}{filename}".ljust(64, "a")[:64],
            status=status,
            chunk_count=len(contents),
        )
        s.add(doc)
        s.commit()
        chunk_ids = []
        for index, text in enumerate(contents):
            chunk = DocumentChunk(
                document_id=doc.id,
                # document_chunks 冗余了一份 user_id（Milvus 的隔离过滤要用），
                # 造数据时得自己填，模型层没有默认值
                user_id=user_id,
                chunk_index=index,
                page_no=pages[index] if index < len(pages) else None,
                content=text,
                token_count=len(text),
            )
            s.add(chunk)
            s.flush()
            chunk_ids.append(chunk.id)
        s.commit()
        doc_id = doc.id
    return doc_id, chunk_ids


def _index_vectors(store: FakeVectorStore, doc_id: int, chunk_ids: list[int], user_id: int = 1):
    store.insert(
        "kb_chunks",
        [
            {"id": cid, "vector": [0.1] * 8, "user_id": user_id, "document_id": doc_id}
            for cid in chunk_ids
        ],
    )


# ---------- 正常检索 ----------


def test_search_finds_chunk_and_returns_references(client, provider, fake_store):
    """主路径：检索命中 → 原文回灌给模型 → 响应里带出处。"""
    doc_id, chunks = _seed_doc(filename="家庭预算.txt", contents=("这个月房贷 4200 元，每月五号扣款。",))
    _index_vectors(fake_store, doc_id, chunks)

    provider.queue(_reply(calls=[_call(query="每个月房贷多少钱")]))
    provider.queue(_reply("你每个月的房贷是 4200 元，每月 5 号扣款。"))

    res = _chat(client, "我预算文件里房贷是多少")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reply"] == "你每个月的房贷是 4200 元，每月 5 号扣款。"
    # 回给模型的那段话里必须有原文，否则模型答对也只是碰巧
    fed = _tool_contents(provider)
    assert "4200" in fed
    assert "家庭预算.txt" in fed
    # 前端渲染引用卡片要的字段
    assert len(body["references"]) == 1
    ref = body["references"][0]
    assert ref["document_id"] == doc_id
    assert ref["chunk_id"] == chunks[0]
    assert ref["filename"] == "家庭预算.txt"
    assert "4200" in ref["snippet"]
    # 这是查询不是写入
    assert body["pending_action"] is None


def test_search_passes_user_filter_to_milvus(client, provider, fake_store):
    """`filter="user_id == N"` 必须真的发给 Milvus —— 少这一行就是数据泄露。"""
    doc_id, chunks = _seed_doc()
    _index_vectors(fake_store, doc_id, chunks)
    provider.queue(_reply(calls=[_call(query="房贷")])).queue(_reply("4200。"))

    _chat(client, "我文件里房贷多少")

    assert fake_store.search_filter == "user_id == 1"
    # 真机上 insert 后立刻读会读到空段，service 靠 Strong 一致性把窗口压到最小。
    # 这条断言防的是「以后有人顺手把这参数删了」——删了单元测试照样全绿
    assert fake_store.consistency_level == "Strong"


def test_cross_user_search_returns_nothing(client, provider, fake_store):
    """需求 §7.7 验收：跨用户检索返回空。

    假引擎不认 filter，于是隔离只能靠 search_chunks 里的 SQL 条件生效 ——
    这正是本用例要测的东西。
    """
    other_doc, other_chunks = _seed_doc(user_id=2, filename="别人的账本.txt", contents=("我有存款 999999 元。",))
    _index_vectors(fake_store, other_doc, other_chunks, user_id=2)
    provider.queue(_reply(calls=[_call(query="存款多少")])).queue(_reply("你的文档里没有找到相关内容。"))

    res = _chat(client, "我文件里存款多少")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["references"] == []
    fed = _tool_contents(provider)
    assert "999999" not in fed
    assert "没有检索到任何相关内容" in fed


def test_page_number_reaches_reference(client, provider, fake_store):
    """PDF 的页码要一路带到引用卡片上 —— 不然用户不知道翻到第几页。"""
    doc_id, chunks = _seed_doc(
        filename="合同.pdf", contents=("租期两年，押金押一付三。",), page_nos=(7,)
    )
    _index_vectors(fake_store, doc_id, chunks)
    provider.queue(_reply(calls=[_call(query="押金怎么付")])).queue(_reply("押一付三。"))

    res = _chat(client, "我合同文件里押金怎么付")

    assert res.json()["references"][0]["page_no"] == 7


# ---------- 只能检索 ready 的 ----------


@pytest.mark.parametrize("status", ["pending", "parsing", "chunking", "embedding", "failed"])
def test_unfinished_documents_are_not_searchable(client, provider, fake_store, status):
    """处理中/失败的文档可能只有半截切块，绝不能进结果。"""
    doc_id, chunks = _seed_doc(status=status, contents=("半截的原文 12345。",))
    _index_vectors(fake_store, doc_id, chunks)
    provider.queue(_reply(calls=[_call(query="原文")])).queue(_reply("没找到。"))

    res = _chat(client, "我文件里那串数字是什么")

    assert res.status_code == 200, res.text
    assert res.json()["references"] == []
    assert "12345" not in _tool_contents(provider)


def test_deleted_document_disappears_from_search(client, provider, fake_store):
    """硬删之后同一个问题检不到 —— 文档行没了，就算向量残留也拼不出正文。"""
    doc_id, chunks = _seed_doc(contents=("这句话很快就会被删掉。",))
    _index_vectors(fake_store, doc_id, chunks)
    with new_session() as s:
        s.delete(s.get(Document, doc_id))
        s.commit()

    provider.queue(_reply(calls=[_call(query="那句话")])).queue(_reply("没找到。"))
    res = _chat(client, "我文件里那句话是什么")

    assert res.json()["references"] == []


# ---------- 空结果 / 故障 ----------


def test_empty_search_tells_model_there_is_nothing(client, provider, fake_store):
    """查不到时的话术很关键：必须明说「没有」，否则模型会拿常识编。"""
    provider.queue(_reply(calls=[_call(query="我的保险单号")])).queue(
        _reply("你的文档里没有找到相关内容。")
    )

    res = _chat(client, "我文件里保险单号是多少")

    fed = _tool_contents(provider)
    assert "没有检索到任何相关内容" in fed
    assert "不要用常识补一个答案" in fed
    assert res.json()["references"] == []


def test_milvus_down_degrades_instead_of_failing(client, provider, fake_store):
    """抠掉向量库：对话照常进行，只是没有引用 —— 知识库不能拖垮聊天。"""
    fake_store.fail_search = True
    provider.queue(_reply(calls=[_call(query="房贷")])).queue(_reply("你的文档里没找到相关内容。"))

    res = _chat(client, "我文件里房贷多少")

    assert res.status_code == 200, res.text
    assert res.json()["references"] == []


def test_blank_query_returns_empty(client, provider, fake_store):
    """模型传了空串：当作没查，不报错。"""
    doc_id, chunks = _seed_doc()
    _index_vectors(fake_store, doc_id, chunks)
    provider.queue(_reply(calls=[_call(query="   ")])).queue(_reply("你可以说得具体一点。"))

    res = _chat(client, "我文件里有什么")

    assert res.status_code == 200, res.text
    assert res.json()["references"] == []


# ---------- 只读：不建卡、不写业务表 ----------


def test_search_never_creates_card_or_rows(client, provider, fake_store):
    """检索是查询：不产生待确认卡片，tasks 一行不多。"""
    with new_session() as s:
        s.add(Task(user_id=1, title="现有待办", status="todo", priority="medium"))
        s.commit()
    doc_id, chunks = _seed_doc()
    _index_vectors(fake_store, doc_id, chunks)
    provider.queue(_reply(calls=[_call(query="房贷")])).queue(_reply("4200。"))

    res = _chat(client, "我文件里房贷多少")

    assert res.status_code == 200, res.text
    assert res.json()["pending_action"] is None
    with new_session() as s:
        assert s.query(PendingAction).count() == 0
        assert s.query(Task).count() == 1


# ---------- 补问：用户明显在问资料，模型却没查 ----------


def test_nudges_when_model_skips_search(client, provider, fake_store):
    """用户说「文件」，模型第一轮不查只回话 → 补问一次，第二轮必须能查到。"""
    doc_id, chunks = _seed_doc(filename="体检报告.txt", contents=("血压 120/80，血脂正常。",))
    _index_vectors(fake_store, doc_id, chunks)

    provider.queue(_reply("我看看。"))  # 第一轮：没调工具
    provider.queue(_reply(calls=[_call(query="体检结果怎么样")]))  # 补问后：调了
    provider.queue(_reply("你的血压是 120/80，血脂正常。"))

    res = _chat(client, "我那份体检报告文件里结果怎么样")

    assert res.status_code == 200, res.text
    assert len(provider.calls) == 3
    assert "search_personal_documents" in provider.calls[1]["messages"][-1]["content"]
    assert len(res.json()["references"]) == 1


def test_no_nudge_for_plain_chat(client, provider, fake_store):
    """闲聊里没提文件：不补问，不检索，一次调用就结束。"""
    provider.queue(_reply("今天天气不错。"))

    res = _chat(client, "今天天气怎么样")

    assert res.status_code == 200, res.text
    assert len(provider.calls) == 1
    assert res.json()["references"] == []


def test_plain_chat_has_empty_references(client, provider, fake_store):
    """老响应口径不能变：普通聊天的 references 是空数组，不是缺字段。"""
    provider.queue(_reply("你好。"))

    body = _chat(client, "你好").json()

    assert body["references"] == []


# ---------- 引用落库（刷新页面后还在） ----------


def _messages_of(session_id: int) -> list[dict]:
    with TestClient(app) as client:
        res = client.get(f"{API}/ai/sessions/{session_id}/messages", params={"order": "desc"})
    assert res.status_code == 200, res.text
    return res.json()["items"]


def test_references_are_persisted_on_the_assistant_message(client, provider, fake_store):
    """刷新后引用卡片不能消失：消息列表要把 references 带回来。"""
    doc_id, chunks = _seed_doc(filename="家庭预算.txt", contents=("这个月房贷 4200 元。",))
    _index_vectors(fake_store, doc_id, chunks)
    provider.queue(_reply(calls=[_call(query="房贷")])).queue(_reply("每月 4200 元。"))

    body = _chat(client, "我文件里房贷多少").json()

    # 聊天接口和消息列表两条路径必须给出同样的一份引用
    items = _messages_of(body["session_id"])
    assistant = next(row for row in items if row["role"] == "assistant")
    assert assistant["id"] == body["message_id"]
    assert assistant["references"] == body["references"]
    assert assistant["references"][0]["chunk_id"] == chunks[0]


def test_messages_without_references_carry_empty_list(client, provider, fake_store):
    """老消息（这一版之前落的）没有引用键，也要返回空数组而不是报错。"""
    provider.queue(_reply("你好。"))

    session_id = _chat(client, "你好").json()["session_id"]

    items = _messages_of(session_id)
    assert items[0]["references"] == []


def test_corrupted_reference_rows_are_skipped(client, provider, fake_store):
    """库里被人手工改坏了，也不能让整个会话打不开：坏的那条跳过，好的照常返回。"""
    provider.queue(_reply(calls=[_call(query="房贷")])).queue(_reply("每月 4200 元。"))
    body = _chat(client, "我文件里房贷多少").json()

    with new_session() as s:
        message = s.get(ChatMessage, body["message_id"])
        message.tool_result = {
            "references": [
                {"document_id": 1, "chunk_id": 2, "filename": "好的.txt", "snippet": "原文"},
                {"document_id": "不是数字"},
                "根本不是对象",
            ]
        }
        s.commit()

    items = _messages_of(body["session_id"])
    assistant = next(row for row in items if row["id"] == body["message_id"])
    assert [row["filename"] for row in assistant["references"]] == ["好的.txt"]


def test_long_answer_keeps_references_alongside_summary(client, provider, fake_store):
    """tool_result 将来若被别的东西占用，引用也不能因此丢失。"""
    doc_id, chunks = _seed_doc(filename="合同.txt", contents=("租期两年，押金三个月房租。",))
    _index_vectors(fake_store, doc_id, chunks)
    provider.queue(_reply(calls=[_call(query="租期多久")])).queue(_reply("租期两年。"))

    body = _chat(client, "我的合同租期多久").json()

    with new_session() as s:
        row = s.get(ChatMessage, body["message_id"])
        assert isinstance(row.tool_result, dict)
        assert len(row.tool_result["references"]) == 1
        assert row.tool_result["references"][0]["filename"] == "合同.txt"