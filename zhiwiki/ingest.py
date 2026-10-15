"""Read only explicitly selected files; never load .env files or external parts."""

import io
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict
from xml.etree import ElementTree


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 500_000
MAX_DOCX_EXPANDED_BYTES = 10 * 1024 * 1024
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class _NoDoctypeBuilder(ElementTree.TreeBuilder):
    """Reject DTDs at the XML parser level, including UTF-16 encoded XML."""

    def doctype(self, name, pubid, system):
        raise ValueError("DOCX 不允许 DTD 或实体定义")


def _check_path(path: str) -> None:
    """Check both separator styles so a Windows path is rejected on any OS."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise ValueError("文件路径不能为空或包含 NUL 字符")
    parts = re.split(r"[\\/]", path)
    if any(part.lower().startswith(".env") for part in parts):
        raise ValueError("禁止读取 .env 文件及其路径变体")


def read_file(path) -> Dict[str, str]:
    """Resolve explicit files before opening to reject symlinks into .env paths."""
    raw_path = str(path)
    _check_path(raw_path)
    source = Path(raw_path).expanduser()
    resolved = source.resolve(strict=True)
    _check_path(str(resolved))
    if not resolved.is_file():
        raise ValueError("导入路径必须是普通文件")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("文件超过 2 MiB 上限")
    _validate_extension(resolved.name)
    with resolved.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    parsed = parse_upload(resolved.name, data)
    parsed["source_key"] = str(resolved)
    return parsed


def parse_upload(filename: str, data: bytes) -> Dict[str, str]:
    """Parse an upload in memory. The filename is metadata, never a write path."""
    _check_path(filename)
    if "/" in filename or "\\" in filename or ":" in filename or filename in (".", ".."):
        raise ValueError("上传文件名不能包含目录或路径组件")
    if not isinstance(data, bytes):
        raise ValueError("文件内容必须是 bytes")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("文件超过 2 MiB 上限")
    basename = PurePosixPath(filename.replace("\\", "/")).name
    suffix = _validate_extension(basename)
    if suffix == ".docx":
        text = _read_docx(data)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Markdown/TXT 必须使用 UTF-8 编码") from error
    _validate_text(text)
    return {"title": Path(basename).stem, "text": text, "source_key": basename}


def _validate_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in (".md", ".txt", ".docx"):
        raise ValueError("仅支持 UTF-8 Markdown、TXT 和 DOCX；不支持 PDF/OCR")
    return suffix


def _validate_text(text: str) -> None:
    if not text.strip():
        raise ValueError("文档内容不能为空")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("正文超过 500,000 字符上限")
    if "\x00" in text:
        raise ValueError("正文不能包含 NUL 字符")


def _read_docx(data: bytes) -> str:
    """Read main-body XML only; bound ZIP expansion and reject DTD/entities."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2048:
                raise ValueError("DOCX 包含过多 ZIP 条目")
            if sum(entry.file_size for entry in entries) > MAX_DOCX_EXPANDED_BYTES:
                raise ValueError("DOCX 展开内容超过 10 MiB 上限")
            body_entries = [entry for entry in entries if entry.filename == "word/document.xml"]
            if len(body_entries) != 1:
                raise ValueError("DOCX 缺少唯一正文 XML")
            if any(entry.flag_bits & 1 for entry in entries):
                raise ValueError("不支持加密 DOCX")
            with archive.open(body_entries[0]) as handle:
                xml = handle.read(MAX_DOCX_EXPANDED_BYTES + 1)
    except (zipfile.BadZipFile, RuntimeError, EOFError, OSError) as error:
        raise ValueError("无法解析 DOCX 压缩包") from error
    if len(xml) > MAX_DOCX_EXPANDED_BYTES:
        raise ValueError("DOCX 正文超过展开上限")
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("DOCX 不允许 DTD 或实体定义")
    try:
        parser = ElementTree.XMLParser(target=_NoDoctypeBuilder())
        root = ElementTree.fromstring(xml, parser=parser)
    except ElementTree.ParseError as error:
        raise ValueError("DOCX 正文 XML 无效") from error
    paragraphs = []
    for paragraph in root.iter(WORD_NS + "p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == WORD_NS + "t":
                parts.append(node.text or "")
            elif node.tag == WORD_NS + "tab":
                parts.append("\t")
            elif node.tag in (WORD_NS + "br", WORD_NS + "cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)
