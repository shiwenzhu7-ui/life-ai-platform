"""文本嵌入 —— 把中文文本变成向量，交给 Milvus 检索。

模型走 fastembed（ONNX Runtime），不装 torch + sentence-transformers：
本机 `fastembed_cache` 里已经有 bge-small-zh-v1.5 的 `model_optimized.onnx`（90MB），
换成 torch 那套要多占约 2.5GB 磁盘和 300–500MB 常驻内存，
而本机 16GB 已插满、可用内存本来就紧（见 docs/项目规划.md S4）。

模型对象是懒加载 + 进程内复用的：第一次 embed 花 ~0.4s 加载权重，
之后每条向量只要 2ms 左右。DEVELOPMENT 下不开 EMBEDDING_WARMUP 会更快见到首屏。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from fastembed import TextEmbedding

from core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _model() -> TextEmbedding:
    """进程内只加载一次。lru_cache 同时挡掉了多线程同时首次加载的浪费。"""
    cache_dir = settings.embedding_cache_dir.strip() or None
    logger.info("加载嵌入模型 %s（缓存目录 %s）", settings.embedding_model, cache_dir or "默认")
    return TextEmbedding(model_name=settings.embedding_model, cache_dir=cache_dir)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量把文本转成向量。返回顺序与入参一致。

    空列表直接返回空列表 —— fastembed 对空输入的行为随版本变过，这里不依赖它。
    """
    if not texts:
        return []
    return [list(map(float, vec)) for vec in _model().embed(texts)]


def embed_query(text: str) -> list[float]:
    """单条查询向量。检索时用这个，不必自己包一层 list。"""
    vectors = embed_texts([text])
    if not vectors:
        raise ValueError("待检索的文本为空")
    return vectors[0]


def warmup() -> None:
    """提前加载模型，让第一次检索不用等。

    启动时调用；失败只记日志不抛 —— 模型加载失败应该让知识库接口自己报错，
    而不是把整个应用（包括待办、记账这些跟向量无关的功能）拖到起不来。
    """
    try:
        embed_texts(["预热"])
        logger.info("嵌入模型已就绪")
    except Exception:  # noqa: BLE001 —— 启动阶段不阻断，留给业务接口按需报错
        logger.exception("嵌入模型预热失败，知识库功能将不可用")


def reset_model() -> None:
    """测试用：丢掉缓存的模型对象。"""
    _model.cache_clear()