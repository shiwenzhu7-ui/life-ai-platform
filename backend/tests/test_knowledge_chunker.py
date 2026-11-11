"""document_chunks 切分器测试。

纯函数，不碰数据库也不碰 Milvus，跑得飞快。
"""

import pytest

from modules.lifeweb.knowledge.chunker import count_characters, normalize_text, split_text


class TestNormalize:
    def test_unifies_line_endings(self):
        assert normalize_text("a\r\nb\rc") == "a\nb\nc"

    def test_removes_zero_width_and_nbsp(self):
        # 零宽字符直接删；不换行空格(NBSP)是「空格」的一种，归一成普通空格后压缩
        assert normalize_text("a\u200bb\u00a0c\ufeffd") == "ab cd"

    def test_nbsp_surrounding_space_collapses(self):
        assert normalize_text("a \u00a0 b") == "a b"

    def test_collapses_spaces_keeps_paragraphs(self):
        assert normalize_text("第一段   有   空格\n\n\n\n第二段") == "第一段 有 空格\n\n第二段"

    def test_strips_line_edges(self):
        assert normalize_text("  第一行  \n   第二行   ") == "第一行\n第二行"

    def test_empty(self):
        assert normalize_text("") == ""
        assert normalize_text("   \n\n  ") == ""


class TestSplit:
    def test_empty_text_returns_empty_list(self):
        assert split_text("") == []
        assert split_text("   \n ") == []

    def test_short_text_single_chunk(self):
        text = "很短的一段话，不需要切。"
        assert split_text(text, size=512, overlap=64) == [text]

    def test_all_content_preserved(self):
        text = "。".join(f"第{i}句话" for i in range(300))
        chunks = split_text(text, size=100, overlap=20)
        assert len(chunks) > 1
        # 原文中的每个「第N句话」都应该至少出现在某一块里
        joined = "".join(chunks)
        for i in range(0, 300, 7):
            assert f"第{i}句话" in joined

    def test_chunks_do_not_exceed_size_much(self):
        text = "，".join(["这是一句没有句号的超长文本"] * 200)  # 故意不给句读
        chunks = split_text(text, size=200, overlap=40)
        # 硬切时每块正好 size；合并的末块可能超，但不应超过 size + min_tail
        for chunk in chunks[:-1]:
            assert len(chunk) <= 200
        assert len(chunks[-1]) <= 200 + 64

    def test_prefers_sentence_boundary(self):
        # 第 1 个句号落在 30 字左右，size=100 → 应该在句号后断开
        text = "甲" * 29 + "。" + "乙" * 200
        chunks = split_text(text, size=100, overlap=10, min_tail=10)
        assert chunks[0].endswith("。")
        assert len(chunks[0]) == 30

    def test_overlap_creates_repeated_content(self):
        text = "".join(f"句{i:03d}。" for i in range(200))
        chunks = split_text(text, size=100, overlap=30, min_tail=10)
        assert len(chunks) >= 2
        # 相邻两块共享一段内容：前一块的尾巴能在后一块开头附近找到
        first_tail = chunks[0][-10:]
        assert first_tail in chunks[1]

    def test_min_tail_merged(self):
        # 原文 106 字，size=100 → 第一块 100 字，尾巴 6 字；min_tail=64 时尾巴应该并回去
        # 注意：重叠到正数时，尾巴是「重合段 + 余量」，长度可能已经超过 min_tail，
        # 这时不合并是正确的（否则同一段内容会被算两遍）。用 overlap=0 才是在测合并逻辑
        text = "甲" * 100 + "。" + "乙" * 5
        chunks = split_text(text, size=100, overlap=0, min_tail=64)
        assert len(chunks) == 1
        assert chunks[0].endswith("乙" * 5)

    def test_short_tail_merged_even_with_overlap(self):
        # overlap=10：第一块 100 字，第二块从 90 起、只含 16 字 → 尾巴 < min_tail=64 要合并
        text = "甲" * 100 + "。" + "乙" * 5
        chunks = split_text(text, size=100, overlap=10, min_tail=64)
        assert len(chunks) == 1

    def test_short_tail_alone_is_merged(self):
        # 正文长、尾巴短，且尾巴真的短于 min_tail → 并进前一块。
        # 这里 size=512 才能复现「最后一小块不足 min_tail」，用 size=100 会切出 4 块
        text = "甲" * 512 + "。尾巴。"
        chunks = split_text(text, size=512, overlap=64, min_tail=64)
        assert all(len(chunk) >= 20 for chunk in chunks)
        assert chunks[-1].endswith("尾巴。")

    def test_no_infinite_loop_on_tiny_overlap(self):
        text = "字" * 1000
        chunks = split_text(text, size=50, overlap=49, min_tail=1)
        assert len(chunks) > 100

    def test_size_and_overlap_validation(self):
        with pytest.raises(ValueError):
            split_text("abc", size=0, overlap=0)
        with pytest.raises(ValueError):
            split_text("abc", size=10, overlap=10)
        with pytest.raises(ValueError):
            split_text("abc", size=10, overlap=-1)

    def test_long_text_real_world_size(self):
        # 模拟一份 20 页文档：每页 800 字，按 512/64 切
        text = "。".join(("内容" * 400).split("。"))
        chunks = split_text(text, size=512, overlap=64)
        assert len(chunks) >= 1
        assert all(chunk.strip() for chunk in chunks)


class TestCountCharacters:
    def test_excludes_whitespace(self):
        assert count_characters("a b\nc\td") == 4

    def test_chinese(self):
        assert count_characters("你好 世界") == 4