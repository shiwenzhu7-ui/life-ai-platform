"""Milvus 客户端封装 —— 整个后端唯一创建 Milvus 连接的地方。

与 core/db.py 的定位一致：连接在进程内复用，别在业务代码里各处 `MilvusClient(...)`。
连接本身是懒建的：import 本模块不会去连虚拟机，第一次真正用到才连
（V1.0/V1.1 的功能不该因为虚拟机没开就起不来）。

为什么 Milvus 跑在虚拟机里：Windows 主机没装 docker，而 Milvus standalone
常驻要 1–2GB；本机 16GB 双槽插满、可用内存本来就紧（见 docs/项目规划.md S4）。
"""

from __future__ import annotations

import logging
from typing import Any

from pymilvus import MilvusClient

from core.config import settings

logger = logging.getLogger(__name__)

# 索引类型/度量：COSINE 对归一化后的嵌入模型最稳（bge 系列训练就是按 cosine 的），
# 且与 L2 相比不受向量模长影响。V1.3 的检索排序按它来。
METRIC_TYPE = "COSINE"

# HNSW 参数。M=16 / efConstruction=200 是官方推荐的中小型集合默认值；
# 个人知识库（万级片段）用这个配置召回与内存都够，不必再调。
_INDEX_PARAMS: dict[str, Any] = {"metric_type": METRIC_TYPE, "index_type": "HNSW", "params": {"M": 16, "efConstruction": 200}}

_client: MilvusClient | None = None


def get_client() -> MilvusClient:
    """返回进程内复用的 Milvus 客户端（懒连接）。

    第一次调用时才建立连接；连接断开（虚拟机重启）时 MilvusClient 自己会重连，
    这里不额外做健康检查 —— 每次检索都探测一次会把延迟翻倍。
    """
    global _client
    if _client is None:
        logger.info("连接 Milvus：%s（库 %s）", settings.vector_uri, settings.vector_db)
        _client = MilvusClient(uri=settings.vector_uri, db_name=settings.vector_db)
    return _client


def ensure_database() -> None:
    """确保目标库存在。

    Milvus 的库（database）是「连接参数」，必须先连到某个已存在的库才能建库，
    所以这里用不带 db_name 的连接去建，建完再让 get_client() 用带 db 的连接。
    """
    admin = MilvusClient(uri=settings.vector_uri)
    try:
        if settings.vector_db not in admin.list_databases():
            admin.create_database(settings.vector_db)
            logger.info("创建 Milvus 库：%s", settings.vector_db)
    finally:
        admin.close()


def ensure_collection() -> None:
    """确保集合存在且结构正确（幂等，可重复调用）。

    向量维度取自 settings.embedding_dim，与嵌入模型绑死：换模型改维度时，
    旧集合不会自动适配，必须 drop 掉重建（V1.3 不做数据迁移）。
    """
    client = get_client()
    name = settings.vector_collection

    if client.has_collection(name):
        desc = client.describe_collection(name)
        # 维度不匹配就明确报错，而不是让后续 insert 抛一个看不懂的 8000 号错误
        for field in desc.get("fields", []):
            if field.get("name") == "vector":
                params = field.get("params") or {}
                existing = params.get("dim")
                if existing is not None and int(existing) != settings.embedding_dim:
                    raise RuntimeError(
                        f"Milvus 集合 {name} 的向量维度是 {existing}，"
                        f"但配置里 EMBEDDING_DIM={settings.embedding_dim}。"
                        "换过嵌入模型的话，需要先 drop 这个集合并重新导入文档。"
                    )
        return

    # auto_id=False：主键用 MySQL 里 document_chunks.id，两边能对上，
    # 删文档时按 chunk_id 精确删，不用额外的映射表。
    client.create_collection(
        collection_name=name,
        dimension=settings.embedding_dim,
        metric_type=METRIC_TYPE,
        auto_id=False,
        index_params=_INDEX_PARAMS,
    )
    logger.info("创建 Milvus 集合：%s（%d 维）", name, settings.embedding_dim)


def reset_connection() -> None:
    """测试用：丢掉缓存的客户端，下次 get_client() 重新连。"""
    global _client
    if _client is not None:
        _client.close()
    _client = None