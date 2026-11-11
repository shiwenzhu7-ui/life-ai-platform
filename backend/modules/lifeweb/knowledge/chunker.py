"""文档切分：把一段长文本切成适合嵌入的块。

参数来自配置（`KB_CHUNK_SIZE` / `KB_CHUNK_OVERLAP`），默认 512 字 / 64 字重叠。

**按字符切，不按 token 切。** 理由：
1. 中文里一个字就是一个语义单位，按字切比分词结果更稳；
2. 不用依赖模型自带 tokenizer 的接口（fastembed 不暴露它）；
3. bge-small 的上下文窗口是 512 token，中文大致对应 500 字上下，
   512 字符是安全的保守值，不怕被截断。

重叠的意义：防止一句话正好被切在两块中间，导致两块各拿一半、谁都搜不到。
64 字足够装下一个完整句子，又不至于让向量库体积虚增太多（512+64 = 12.5%）。

**边界优先**：尽量在句读处断开，而不是数到第 512 个字就硬切。
一段话从中间劈开，嵌入出来的向量会两头不像。实现是「在块里从后往前找第一个
句读符号，且切点不早于 start + 20 字」，找不到才硬切。
"""

from __future__ import annotations

import re

# 中文与英文的句读符号
_BREAK_CHARS = "。！？；!?;\n"

# 一块最少这么多字。低于这个数就从 hard_end 硬切：
# 正文里本来也找不到「靠句号切出来只剩 10 个字」的合理情况，
# 真出现了通常说明这块是标题/图注/列表项，硬切反而更完整
MIN_CHUNK_CHARS = 20

# 空白压缩：PDF 抽出来的文本经常带大量空格与空行，保留它们会让字符数虚高、
# 切出来的块里一半是空白。连续的空白（含换行）压成一个空格，
# 但**保留段落之间的换行**——段落边界对语义有影响
_MULTI_SPACE = re.compile(r"[ \t\u3000]+")
_MULTI_NEWLINE = re.compile(r"\n{2,}")


def normalize_text(text: str) -> str:
    """把解析出来的原始文本压成适合切分的样子。

    - 统一换行符（Windows 的 \\r\\n、老 Mac 的 \\r 都变成 \\n）
    - 去掉零宽字符与 Unicode 不换行空格（PDF 里很常见）
    - 制表符与连续空格压成一个
    - 三个以上连续换行压成两个（= 一个空行，保留段落分隔）
    - 去掉首尾空白
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    # 每行首尾的空格去掉，但不合并行与行（段落结构要留着）
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip()


def split_text(
    text: str,
    size: int = 512,
    overlap: int = 64,
    min_tail: int = 64,
) -> list[str]:
    """把文本切成若干块。

    参数：
        text     已经 normalize 过的文本
        size     每块目标字符数
        overlap  相邻块的重叠字符数（search 窗口大小的一半左右比较合适）
        min_tail 最后一块短于这个长度就并进前一块，避免出现「就剩两个字的碎片」

    返回：非空字符串列表，按原文顺序。空文本返回 []。

    实现要点：
        每块的硬上限是 size。块尾落在哪里，按优先级：
          1. [start + 20, start + size] 里最靠后的句读符号
          2. 都没有 → 在 start + size 处硬切
        下一块的 start = 本块结束位置 - overlap，所以相邻块一定有重叠，
        但**不保证精确等于** overlap（切在句号处时以句号为准）。
    """
    if size <= 0:
        raise ValueError("size 必须大于 0")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap 必须大于等于 0 且小于 size")

    text = normalize_text(text)
    if not text:
        return []

    chunks: list[str] = []
    length = len(text)
    start = 0
    # 块尾绝不能早于 start + MIN_CHUNK_CHARS。
    # 没有这条约束，「开头就是句号」的文本（标题、图注、列表）会每块只切出
    # 「正文。两个」这种 10 字碎块，整篇被切成一堆碎片。
    lower_bound = min(MIN_CHUNK_CHARS, size)

    while start < length:
        hard_end = min(start + size, length)

        if hard_end >= length:
            # 剩下不足一块，直接收尾
            piece = text[start:length].strip()
            if piece:
                chunks.append(piece)
            break

        # 句读优先：在 [start, hard_end] 里从后往前找第一个句读，
        # 但切点必须 >= start + lower_bound（防止切出太碎的块）。
        cut = _find_break(text, start, hard_end, limit=start + lower_bound)
        end = cut if cut is not None else hard_end

        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)

        # 下一块的起点 = 本块结尾往前走 overlap。
        # 用 max(start + 1, ...) 兜底，保证一定会前进，不会死循环
        start = max(start + 1, end - overlap)

    # 末块过短就并进前一块（并完可能超过 size，这是有意的：
    # 一个 20 字的尾巴单独成块，嵌入质量反而更差）
    if len(chunks) >= 2 and len(chunks[-1]) < min_tail:
        tail = chunks.pop()
        chunks[-1] = f"{chunks[-1]}\n{tail}"

    return chunks


def _find_break(text: str, start: int, end: int, limit: int) -> int | None:
    """在 text[start:end] 里找**最靠后**的句读符号，但只接受位置 >= limit 的。

    返回该符号之后的位置（块尾）。返回 None 表示没有可用的句读，调用方应当硬切。

    为什么要有 limit：
        「别把块切得太短」这条规矩两处都要守，但守的是**切点到 start 的距离**，
        不是**句号出现的位置**。反例：`甲×29 + 。+ 乙×200`，size=100。
        句号在 30，若要求「句号必须出现在 start+45 之后」，这个句号就被无视了，
        前 100 字硬切，把一句话劈成两半。正确行为是切在 30。

    为什么不能「先找句号的最后一个、再找逗号的最后一个、取较远者」：
        那样会把「甲。乙。丙，丁」切到逗号处，跨过两个完整句子。
        这里从后往前扫，遇到任意一个句读就返回。
    """
    for pos in range(min(end, len(text)) - 1, start - 1, -1):
        if text[pos] in _BREAK_CHARS and pos + 1 >= limit:
            return pos + 1
    return None


def count_characters(text: str) -> int:
    """统计正文字符数（不含空白）。

    存进 `document_chunks.token_count` —— 列名历史原因叫 token，
    实际口径见 `document_chunk.py` 的注释。
    """
    return len(re.sub(r"\s", "", text))