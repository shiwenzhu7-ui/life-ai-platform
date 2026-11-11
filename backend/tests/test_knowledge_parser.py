"""document 解析器测试。

真文件用 pypdf / python-docx **现场生成**，不往仓库里放二进制 fixture——
这样测试文件本身可读、可 diff，也不会因为某个 PDF 工具版本变化而莫名其妙挂掉。
"""

import io

import docx
import pytest
from pypdf import PdfWriter

from modules.lifeweb.knowledge.parser import (
    ParseError,
    guess_ext,
    parse_bytes,
    sanitize_filename,
    sha256_of,
)


# ---------------------------------------------------------------- 造测试文件


def make_pdf(pages: list[str]) -> bytes:
    """生成一份每页一段文字的 PDF。

    注意：pypdf 只能**写结构**不能排版正文，所以这里用 pypdf 的底层对象手搓
    一个最小可用的页面（含文本流）。Latin-1 字符集，中文写不进去——
    中文 PDF 的解析靠真实文件验证，单测里用英文足够覆盖「分页 + 抽文字」这条链路。
    """
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        content = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content.set_data(
            f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        )
        page[NameObject("/Contents")] = writer._add_object(content)  # noqa: SLF001
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}  # noqa: SLF001
                )
            }
        )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for col_index, cell in enumerate(row):
                grid.cell(row_index, col_index).text = cell
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------- 分发


class TestParseDispatch:
    def test_unknown_extension_rejected(self):
        with pytest.raises(ParseError, match="不支持的文件类型"):
            parse_bytes(b"whatever", "md")

    def test_extension_case_and_dot_insensitive(self):
        doc = parse_bytes("你好".encode(), ".TXT")
        assert doc.pages[0].text == "你好"


# ---------------------------------------------------------------- TXT


class TestParseTxt:
    def test_utf8(self):
        doc = parse_bytes("第一行\n第二行".encode(), "txt")
        assert len(doc.pages) == 1
        assert doc.pages[0].page_no is None
        assert doc.pages[0].text == "第一行\n第二行"
        assert doc.warnings == []

    def test_utf8_with_bom_has_no_residue(self):
        doc = parse_bytes("\ufeff开头".encode("utf-8-sig"), "txt")
        assert doc.pages[0].text == "开头"

    def test_gbk_falls_back_and_warns(self):
        doc = parse_bytes("这是一段用 GBK 编码的中文".encode("gbk"), "txt")
        assert "GBK" in doc.pages[0].text or "中文" in doc.pages[0].text
        assert any("编码" in warning for warning in doc.warnings)

    def test_empty_file_rejected(self):
        with pytest.raises(ParseError, match="空的"):
            parse_bytes(b"", "txt")

    def test_whitespace_only_rejected(self):
        with pytest.raises(ParseError, match="空白"):
            parse_bytes(b"   \n\t  ", "txt")


# ---------------------------------------------------------------- DOCX


class TestParseDocx:
    def test_paragraphs(self):
        data = make_docx(["第一段", "第二段", ""])
        doc = parse_bytes(data, "docx")
        assert len(doc.pages) == 1
        assert doc.pages[0].page_no is None
        assert doc.pages[0].text == "第一段\n第二段"

    def test_tables_are_included(self):
        # 合同/报表里的关键数字常在表格里，不读表格等于漏掉最该检索的内容
        data = make_docx(["月度报表"], table=[["项目", "金额"], ["房租", "3000"]])
        doc = parse_bytes(data, "docx")
        text = doc.pages[0].text
        assert "月度报表" in text
        assert "房租 | 3000" in text

    def test_empty_docx_rejected(self):
        with pytest.raises(ParseError, match="没有文字"):
            parse_bytes(make_docx([]), "docx")

    def test_broken_zip_rejected(self):
        with pytest.raises(ParseError, match="DOCX 文件无法打开"):
            parse_bytes(b"not a zip at all", "docx")


# ---------------------------------------------------------------- PDF


class TestParsePdf:
    def test_multipage_keeps_page_numbers(self):
        data = make_pdf(["page one content", "page two content"])
        doc = parse_bytes(data, "pdf")
        assert [page.page_no for page in doc.pages] == [1, 2]
        assert "page one" in doc.pages[0].text
        assert "page two" in doc.pages[1].text

    def test_blank_pages_skipped(self):
        data = make_pdf(["only content", ""])
        doc = parse_bytes(data, "pdf")
        # 空页不产生 page，但页码仍然按原始页序保留
        assert len(doc.pages) == 1
        assert doc.pages[0].page_no == 1

    def test_not_a_pdf_rejected(self):
        with pytest.raises(ParseError, match="PDF 文件无法打开"):
            parse_bytes(b"definitely not a pdf", "pdf")

    def test_pdf_without_text_rejected(self):
        # 无文字层的 PDF（扫描件的简化版）应给出明确提示，而不是静默产出空文档
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buffer = io.BytesIO()
        writer.write(buffer)
        with pytest.raises(ParseError, match="没有可提取的文字"):
            parse_bytes(buffer.getvalue(), "pdf")


# ---------------------------------------------------------------- 工具


class TestHelpers:
    def test_sha256_detects_content_not_name(self):
        assert sha256_of(b"abc") == sha256_of(b"abc")
        assert sha256_of(b"abc") != sha256_of(b"abd")

    def test_sha256_is_64_hex(self):
        digest = sha256_of(b"x")
        assert len(digest) == 64
        assert all(ch in "0123456789abcdef" for ch in digest)


class TestSanitizeFilename:
    def test_plain_name_unchanged(self):
        assert sanitize_filename("季度报告.pdf") == "季度报告.pdf"

    def test_path_traversal_stripped(self):
        assert sanitize_filename("../../../etc/passwd") == "passwd"
        assert sanitize_filename(r"..\..\windows\system32\config") == "config"

    def test_windows_illegal_chars(self):
        assert sanitize_filename('a<b>c:d"e|f?g*h.txt') == "a_b_c_d_e_f_g_h.txt"

    def test_control_chars_removed(self):
        # \x00 先被 replace 成空串；\x1f 属于「非法字符」那一类，替换成下划线
        assert sanitize_filename("a\x00b\x1fc.txt") == "ab_c.txt"
        assert sanitize_filename("报告\x00终稿.pdf") == "报告终稿.pdf"

    def test_trims_dots_and_spaces(self):
        assert sanitize_filename("  报告.txt.  ") == "报告.txt"

    def test_empty_falls_back(self):
        assert sanitize_filename("") == "untitled"
        assert sanitize_filename("   ") == "untitled"

    def test_fullwidth_normalized(self):
        # NFKC 会把全角括号/字母转成半角，避免同一份文件因输入法产生两个名字
        assert sanitize_filename("报告（终稿）.TXT") == "报告(终稿).TXT"

    def test_long_name_truncated_keeps_extension(self):
        name = sanitize_filename("长" * 400 + ".pdf")
        assert len(name) <= 200
        assert name.endswith(".pdf")

    def test_guess_ext(self):
        assert guess_ext("报告.PDF") == "pdf"
        assert guess_ext("a.tar.gz") == "gz"
        assert guess_ext("noext") == ""