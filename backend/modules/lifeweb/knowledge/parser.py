"""文档解析：把上传的文件读成「带页码的纯文本段」。

三种格式，三条路：

| 格式 | 库 | 页码 |
|---|---|---|
| PDF  | pypdf        | 有（按页读，`page_no` 从 1 开始） |
| DOCX | python-docx  | 无（Word 的分页是渲染时才定的，文件里没有可靠的页码） |
| TXT  | 内置 codec   | 无 |

返回统一结构 `ParsedDocument(pages=[ParsedPage(page_no, text), ...], warnings=[...])`：
PDF 每页一段，DOCX/TXT 整篇算一页（`page_no=None`）。
切分器再把每一页各自切块 —— 这样一句话不会被切成「前半在 P3、后半在 P4」。

**为什么在这里就按页切开而不是拼成一大篇再切**：引用卡片要显示「第 3 页」，
如果先合并再切，块和页的对应关系就丢了，得额外记账。

错误处理：解析失败抛 `ParseError`，消息是给人看的一句话（不含堆栈），
由调用方写进 `documents.error_message`。
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class ParseError(Exception):
    """解析失败。message 直接展示给用户，不要塞 Traceback。"""


@dataclass
class ParsedPage:
    page_no: int | None
    text: str


@dataclass
class ParsedDocument:
    pages: list[ParsedPage] = field(default_factory=list)
    # 非致命问题，比如「第 5 页是空页」「检测到 GBK 编码」
    warnings: list[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(len(page.text) for page in self.pages)


def parse_bytes(data: bytes, file_ext: str) -> ParsedDocument:
    """按扩展名分发。`file_ext` 不含点，小写，如 `pdf`。"""
    ext = file_ext.lower().lstrip(".")
    if ext == "pdf":
        return _parse_pdf(data)
    if ext == "docx":
        return _parse_docx(data)
    if ext == "txt":
        return _parse_txt(data)
    raise ParseError(f"不支持的文件类型：.{ext}（只支持 pdf / docx / txt）")


# ---------------------------------------------------------------- PDF


def _parse_pdf(data: bytes) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover —— 依赖缺失属于部署问题
        raise ParseError("缺少 pypdf，无法解析 PDF") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # 空密码能解的商品 PDF 不少，先试一次；解不开才报错
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise ParseError("PDF 已加密，无法读取") from exc
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 坏文件、非 PDF 伪装成 .pdf 都走这里
        raise ParseError(f"PDF 文件无法打开：{exc}") from exc

    if not reader.pages:
        raise ParseError("PDF 没有任何页面")

    doc = ParsedDocument()
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 —— 单页坏掉不该毁掉整份文件
            doc.warnings.append(f"第 {index} 页解析失败：{type(exc).__name__}")
            continue
        text = text.strip()
        if not text:
            continue
        doc.pages.append(ParsedPage(page_no=index, text=text))

    if not doc.pages:
        raise ParseError("PDF 里没有可提取的文字（可能是扫描件，需要 OCR —— V1.3 不做 OCR）")
    return doc


# ---------------------------------------------------------------- DOCX


def _parse_docx(data: bytes) -> ParsedDocument:
    try:
        import docx  # python-docx
    except ImportError as exc:  # pragma: no cover
        raise ParseError("缺少 python-docx，无法解析 DOCX") from exc

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 —— 老 .doc 格式、损坏的 zip 都走这里
        raise ParseError(f"DOCX 文件无法打开：{exc}") from exc

    paragraphs = [p.text.strip() for p in document.paragraphs]
    paragraphs = [p for p in paragraphs if p]
    # 表格里的文字也算正文：合同、报表类文档的关键数字常在表格里，
    # 不读表格等于漏掉最该检索的内容
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            cells = [c for c in cells if c]
            if cells:
                paragraphs.append(" | ".join(cells))

    if not paragraphs:
        raise ParseError("DOCX 里没有文字内容")

    # Word 的分页是渲染时算的，文件里没有可靠页码，整篇算一页
    return ParsedDocument(pages=[ParsedPage(page_no=None, text="\n".join(paragraphs))])


# ---------------------------------------------------------------- TXT

# 依次尝试的编码。utf-8-sig 放最前：带 BOM 的文件用纯 utf-8 读会多出一个 \ufeff
_TXT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5")
# 零宽字符与 BOM：utf-8-sig 只能去掉文件开头的那个，正文中间的还得单独清
_INVISIBLE = re.compile(r"[\ufeff\u200b\u200c\u200d\ufeff]")


def _parse_txt(data: bytes) -> ParsedDocument:
    if not data:
        raise ParseError("文件是空的")

    text: str | None = None
    used: str | None = None
    for encoding in _TXT_ENCODINGS:
        try:
            text = data.decode(encoding)
            used = encoding
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        raise ParseError("文本编码无法识别（试过 UTF-8 / GB18030 / Big5）")

    text = _INVISIBLE.sub("", text)

    if not text.strip():
        raise ParseError("文件里只有空白字符")

    doc = ParsedDocument(pages=[ParsedPage(page_no=None, text=text)])
    if used not in ("utf-8-sig", "utf-8"):
        doc.warnings.append(f"文件不是 UTF-8 编码，已按 {used} 读取")

    return doc


# ---------------------------------------------------------------- 工具


def sha256_of(data: bytes) -> str:
    """内容指纹，用于同用户去重。"""
    return hashlib.sha256(data).hexdigest()


# Windows 不允许出现在文件名里的字符，外加上控制字符
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_MAX_FILENAME = 200


def sanitize_filename(name: str) -> str:
    """把上传的原始文件名净化成一个可安全展示、可安全落盘的名字。

    做的事：
      1. 只取最后一段 —— 防 `../../etc/passwd` 这类路径穿越
      2. NFKC 归一化（全角转半角、兼容字符统一），顺便把各种冒牌斜杠变回正常字符
      3. 去掉 Windows 非法字符与控制字符
      4. 去掉首尾的点和空格（Windows 上 `a.` 存不进去）
      5. 限长 200，保留扩展名
      6. 名字空了就用 `untitled`

    注意：结果的用途只有「展示」和「猜扩展名」，
    **真正落盘用的文件名是随机 uuid**（见 service 层），不用这个。
    """
    name = (name or "").replace("\x00", "")
    # 浏览器可能传完整路径（老 IE 风格），统一按两种分隔符取最后一段
    name = name.replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFKC", name)
    name = _ILLEGAL_FILENAME.sub("_", name)
    name = name.strip().strip(".")
    name = re.sub(r"\s+", " ", name)

    if not name:
        return "untitled"

    if len(name) > _MAX_FILENAME:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            keep = _MAX_FILENAME - len(ext) - 1
            name = f"{stem[:keep]}.{ext}"
        else:
            name = name[:_MAX_FILENAME]
    return name


def guess_ext(filename: str) -> str:
    """从文件名取扩展名（小写、不含点）。没有扩展名返回空串。"""
    return Path(filename).suffix.lower().lstrip(".")