"""端到端冒烟：真实 MySQL + 真实 Milvus + 真实嵌入模型。

跑法（backend 目录下）：
    .\\.venv\\Scripts\\python.exe scripts\\smoke_knowledge.py

做的事：
  1. 用演示账号 demo/demo1234 登录拿令牌（走真实 /auth/login）
  2. 上传一份中文 TXT（走真实 /knowledge/documents + BackgroundTasks）
  3. 轮询详情，等 status 变 ready
  4. 直接查 Milvus，确认向量条数 = 切块数
  5. 删除文档，确认 Milvus 里也清空了

⚠️ 会往真实库 lifeai 里写一条文档再删掉。文档名以 __smoke__ 开头，便于辨认。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402

from core.config import settings  # noqa: E402
from core.vectorstore import get_client  # noqa: E402

BASE = "http://127.0.0.1:8010/api/v1"

TEXT = """
季度工作总结。本季度共完成 12 个需求，其中 3 个是紧急线上问题。
最大的收获是把发布流程从手工改成了自动化，平均上线时间从两天缩短到三十分钟。
下一步计划是补齐监控告警，重点是数据库慢查询和接口超时这两类问题。
另外团队新来了两位同学，需要安排一次代码规范的讲解。
""".strip()


def main() -> int:
    with httpx.Client(timeout=120) as c:
        # 1. 登录
        r = c.post(f"{BASE}/auth/login", json={"username": "demo", "password": "demo1234"})
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print("① 登录成功")

        # 2. 上传
        files = {"file": ("__smoke__工作总结.txt", TEXT.encode("utf-8"), "text/plain")}
        r = c.post(f"{BASE}/knowledge/documents", files=files, headers=headers)
        print("② 上传 ->", r.status_code, r.json())
        if r.status_code == 409:
            print("   这份文件已经传过了（说明上次冒烟留下的记录还在），直接删掉重来")
            doc_id = r.json()["detail"]["document_id"]
            c.delete(f"{BASE}/knowledge/documents/{doc_id}", headers=headers)
            r = c.post(f"{BASE}/knowledge/documents", files=files, headers=headers)
            print("   重传 ->", r.status_code)
        assert r.status_code == 202, r.text
        doc_id = r.json()["id"]

        # 3. 轮询
        for i in range(60):
            time.sleep(1)
            d = c.get(f"{BASE}/knowledge/documents/{doc_id}", params={"chunk_limit": 0}, headers=headers).json()
            status = d["document"]["status"]
            if status in ("ready", "failed"):
                break
            if i % 5 == 0:
                print(f"   轮询中… {status}")
        print("③ 最终状态 ->", status, "| 块数", d["document"]["chunk_count"], "|", d["document"]["error_message"])
        assert status == "ready", "处理失败，看 backend\\_verify_srv.err"

        # 4. 详情里的切块
        detail = c.get(f"{BASE}/knowledge/documents/{doc_id}", headers=headers).json()
        print("④ 切块预览：")
        for chunk in detail["chunks"]:
            print(f"   [{chunk['chunk_index']}] {chunk['content'][:40]}… ({chunk['token_count']} 字)")

        # 5. Milvus 里的向量
        client = get_client()
        client.load_collection(settings.vector_collection)
        rows = client.query(
            collection_name=settings.vector_collection,
            filter=f"document_id == {doc_id}",
            output_fields=["id", "user_id", "document_id"],
            limit=100,
        )
        print(f"⑤ Milvus 里的向量条数 -> {len(rows)}")
        assert len(rows) == d["document"]["chunk_count"], "Milvus 与 MySQL 块数不一致！"
        chunk_ids = {c["id"] for c in detail["chunks"]}
        assert {r["id"] for r in rows} <= chunk_ids, "Milvus 主键与 MySQL 块 id 对不上！"
        print("   主键核对：Milvus id 全部来自 document_chunks.id ✅")

        # 6. 删除
        r = c.delete(f"{BASE}/knowledge/documents/{doc_id}", headers=headers)
        print("⑥ 删除 ->", r.status_code)
        assert r.status_code == 204

        time.sleep(1)
        rows = client.query(
            collection_name=settings.vector_collection,
            filter=f"document_id == {doc_id}",
            output_fields=["id"],
            limit=100,
        )
        print(f"⑦ 删除后 Milvus 剩余 -> {len(rows)}")
        assert len(rows) == 0, "删除后 Milvus 里还有向量！"

        again = c.get(f"{BASE}/knowledge/documents/{doc_id}", headers=headers)
        print("⑧ 删除后再查详情 ->", again.status_code)
        assert again.status_code == 404

    print("\n全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())