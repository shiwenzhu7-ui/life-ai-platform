"""V1.3 地基的连通性自检 —— 不属于 pytest（会连虚拟机、写真实 Milvus 集合）。

跑法（backend 目录下）：
    .\\.venv\\Scripts\\python.exe scripts\\check_rag_env.py

它会依次检查：Milvus 连通 → 建库建集合 → 写 3 条真向量 → 检索 → 按 user_id 过滤 →
删干净。任何一步失败都会打印可复制的报错并返回非 0 退出码。

为什么要这个脚本：Milvus 在虚拟机里、IP 由 DHCP 分配、Windows 上没 docker，
出问题时先分清是「虚拟机没开」「Milvus 没起」「防火墙拦了」还是「代码写错了」，
比在接口层调试快得多。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 GBK，打印 ✓ 会 UnicodeEncodeError —— 脚本里先把自己切到 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import embedding, vectorstore  # noqa: E402
from core.config import settings  # noqa: E402

PROBES = [
    "今天下午三点开产品评审会",
    "明天去银行取钱",
    "周末和朋友去打羽毛球",
]


def main() -> int:
    print(f"配置：VECTOR_URI={settings.vector_uri} DB={settings.vector_db} COLL={settings.vector_collection}")
    print(f"      MODEL={settings.embedding_model} DIM={settings.embedding_dim}")

    print("\n[1/5] 连 Milvus …")
    vectorstore.ensure_database()
    client = vectorstore.get_client()
    print(f"      服务端版本 {client.get_server_version()}，库列表 {client.list_databases()}")

    print("[2/5] 确保集合存在 …")
    vectorstore.ensure_collection()
    name = settings.vector_collection
    print(f"      集合 {name} 就绪，当前行数 {client.query(name, filter='id >= 0', output_fields=['count(*)'])[0]['count(*)']}")

    print("[3/5] 嵌入 3 条探针文本 …")
    vectors = embedding.embed_texts(PROBES)
    print(f"      得到 {len(vectors)} 条向量，维度 {len(vectors[0])}")
    assert len(vectors[0]) == settings.embedding_dim, "维度与 EMBEDDING_DIM 不一致"

    print("[4/5] 写入 + 检索 …")
    probe_ids = [-1, -2, -3]  # 负数主键，避免和真实的 document_chunks.id 撞车
    try:
        client.insert(
            name,
            [
                {"id": pid, "user_id": 1, "text": txt, "vector": vec}
                for pid, txt, vec in zip(probe_ids, PROBES, vectors)
            ],
        )
        client.flush(name)
        hits = client.search(name, data=[vectors[0]], limit=3, output_fields=["text", "user_id"])
        for hit in hits[0]:
            print(f"      {hit['id']}  cos={hit['distance']:.4f}  {hit['entity']['text']}")
        if hits[0][0]["id"] != -1:
            print("      ⚠ 自己没排第一，检查 metric_type 或维度")
        else:
            print("      ✓ 自己的向量排第一")

        print("[5/5] 按 user_id 过滤 + 清理 …")
        filtered = client.search(
            name, data=[vectors[0]], limit=3, filter="user_id == 2", output_fields=["user_id"]
        )
        print(f"      user_id==2 命中 {len(filtered[0])} 条（应为 0，刚才只写了 user_id=1）")
        client.delete(name, ids=probe_ids)
        print("      探针数据已删除")
    except Exception:
        # 失败也尽量清理，别把脏数据留在集合里
        try:
            client.delete(name, ids=probe_ids)
        except Exception:  # noqa: BLE001
            pass
        raise

    print("\n全部通过：Milvus 连通、模型可用、写入检索过滤都正常。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 —— 脚本的职责就是把错误原样打出来
        print(f"\n失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)