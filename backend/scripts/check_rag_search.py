"""真实环境冒烟：检索 → 拼 references（不启动 HTTP，直接调函数）。

用真 Milvus + 真 fastembed。验证三件事：
1. search_chunks 能检到刚写进去的切块，正文从 MySQL 回来
2. 改了 user_id 过滤就检不到（隔离在真引擎上成立）
3. _collect_references 的字段与前端约定一致
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import delete  # noqa: E402

from core import vectorstore  # noqa: E402
from core.config import settings  # noqa: E402
from core.db import SessionLocal  # noqa: E402
from modules.lifeweb.ai.tool_flow import _collect_references, _fold_search_result  # noqa: E402
from modules.lifeweb.knowledge import service  # noqa: E402
from modules.lifeweb.knowledge.models.document import Document  # noqa: E402
from modules.lifeweb.knowledge.models.document_chunk import DocumentChunk  # noqa: E402

PROBE_IDS = (999001, 999002)


def main() -> None:
    vectorstore.ensure_collection()
    client = vectorstore.get_client()

    # 清掉上次的探针
    for uid in (9001, 9002):
        client.delete(collection_name=settings.vector_collection, filter=f"user_id == {uid}")

    with SessionLocal() as db:
        db.execute(delete(DocumentChunk).where(DocumentChunk.id.in_(PROBE_IDS)))
        db.execute(delete(Document).where(Document.id.in_((999001 - 997001, 999002 - 997001))))
        db.commit()

        texts = {
            999001: ("真实冒烟：家庭预算.txt", "这个月房贷 4200 元，每月五号从工资卡扣款。", 9001),
            999002: ("真实冒烟：别人的.txt", "我的存款是 999999 元。", 9002),
        }
        docs = {}
        for chunk_id, (filename, content, user_id) in texts.items():
            doc = Document(
                # 文档 id 与切块 id 分开编号，避免和库里已有数据撞上
                id=chunk_id - 997001,
                user_id=user_id,
                filename=filename,
                file_ext="txt",
                file_size=len(content),
                stored_path=f"{user_id}/{chunk_id}.txt",
                sha256=f"{chunk_id}".ljust(64, "0"),
                status="ready",
                chunk_count=1,
            )
            db.add(doc)
            db.flush()
            chunk = DocumentChunk(
                id=chunk_id,
                document_id=doc.id,
                user_id=user_id,
                chunk_index=0,
                page_no=3,
                content=content,
                token_count=len(content),
            )
            db.add(chunk)
            db.flush()
            docs[chunk_id] = (doc.id, content, user_id)
        db.commit()

        rows = [
            {
                "id": cid,
                "vector": service.embedding.embed_texts([content])[0],
                "user_id": user_id,
                "document_id": doc_id,
            }
            for cid, (doc_id, content, user_id) in docs.items()
        ]
        client.insert(collection_name=settings.vector_collection, data=rows)

        hits = service.search_chunks(db, 9001, "每个月房贷多少钱")
        assert len(hits) == 1, hits
        assert hits[0]["filename"] == "真实冒烟：家庭预算.txt"
        assert "4200" in hits[0]["content"]
        assert hits[0]["page_no"] == 3
        print(f"[1] 本人检索命中 {len(hits)} 段，score={hits[0]['score']}")

        other = service.search_chunks(db, 7777, "每个月房贷多少钱")
        assert other == [], f"跨用户检索不该有结果：{other}"
        print("[2] 别的用户检索为空 ✓")

        refs = _collect_references([{"tool": "search_personal_documents", "items": hits}])
        assert refs and set(refs[0]) == {
            "document_id",
            "chunk_id",
            "filename",
            "page_no",
            "chunk_index",
            "snippet",
            "score",
        }
        print(f"[3] references 字段：{sorted(refs[0])}")

        folded = _fold_search_result({"query": "每个月房贷多少钱"}, {"count": 1, "items": hits})
        assert "4200" in folded and "第 3 页" in folded
        print("[4] 回灌文案：")
        print(folded)

        empty = _fold_search_result({"query": "x"}, {"count": 0, "items": []})
        assert "没有检索到任何相关内容" in empty

        # 清理
        for uid in (9001, 9002):
            client.delete(collection_name=settings.vector_collection, filter=f"user_id == {uid}")
        db.execute(delete(DocumentChunk).where(DocumentChunk.id.in_(PROBE_IDS)))
        db.execute(delete(Document).where(Document.id.in_(tuple(d for d, _, _ in docs.values()))))
        db.commit()

    print("\n全部通过")


if __name__ == "__main__":
    main()