"""Safe file parsing checks; synthetic inputs never read user configuration."""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from zhiwiki.ingest import MAX_DOCX_EXPANDED_BYTES, MAX_FILE_BYTES, parse_upload, read_file


def docx_package(xml, extras=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        for name, value in (extras or {}).items():
            archive.writestr(name, value)
    return buffer.getvalue()


class IngestTests(unittest.TestCase):
    def test_utf8_markdown_and_bom(self):
        data = "# 退货规则\n七天内可退货。".encode("utf-8-sig")
        parsed = parse_upload("规则.md", data)
        self.assertEqual(parsed["title"], "规则")
        self.assertEqual(parsed["source_key"], "规则.md")
        self.assertTrue(parsed["text"].startswith("# 退货"))

    def test_explicit_file_returns_stable_resolved_source_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.txt"
            path.write_text("Warranty: two years.", encoding="utf-8")
            parsed = read_file(path)
            self.assertEqual(parsed["source_key"], str(path.resolve()))
            self.assertIn("two years", parsed["text"])

    def test_env_components_rejected_before_filesystem_access(self):
        for path in (".env", ".ENV.txt", "folder/.env.prod/notes.txt", "folder\\.ENV\\notes.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                read_file(path)
            with self.subTest(upload=path), self.assertRaises(ValueError):
                parse_upload(path, b"not opened")

    def test_upload_paths_and_unsupported_files_rejected(self):
        for filename in ("../../policy.md", "folder\\policy.txt", "C:policy.txt", "paper.pdf", "macro.docm"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                parse_upload(filename, b"text")

    def test_reject_encoding_size_and_empty_content(self):
        for content in (b"\xff\xfe", b"   \n", b"x" * (MAX_FILE_BYTES + 1), b"x" * 500001, b"a\x00b"):
            with self.subTest(length=len(content)), self.assertRaises(ValueError):
                parse_upload("policy.txt", content)

    def test_docx_paragraphs_tabs_breaks_and_tables(self):
        xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
        <w:p><w:r><w:t>Returns</w:t><w:tab/><w:t>seven days</w:t><w:br/><w:t>keep receipt</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Warranty</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>'''
        result = parse_upload("policy.docx", docx_package(xml))
        self.assertEqual(result["text"], "Returns\tseven days\nkeep receipt\nWarranty")

    def test_docx_rejects_entities_in_utf8_and_utf16(self):
        template = '''<?xml version="1.0" encoding="{encoding}"?>
        <!DOCTYPE document [<!ENTITY secret "SHOULD_NOT_EXPAND">]>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
        <w:p><w:r><w:t>&secret;</w:t></w:r></w:p></w:body></w:document>'''
        for encoding in ("utf-8", "utf-16"):
            payload = template.format(encoding=encoding).encode(encoding)
            with self.subTest(encoding=encoding), self.assertRaises(ValueError):
                parse_upload("entity.docx", docx_package(payload))

    def test_docx_expansion_limit_and_missing_xml(self):
        package = docx_package(b"<document/>", {"padding.bin": b"0" * (MAX_DOCX_EXPANDED_BYTES + 1)})
        self.assertLess(len(package), MAX_FILE_BYTES)
        with self.assertRaises(ValueError):
            parse_upload("large.docx", package)
        for data in (b"not zip", docx_package(b"<broken>")):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                parse_upload("broken.docx", data)


if __name__ == "__main__":
    unittest.main()
