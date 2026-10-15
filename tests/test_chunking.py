"""Verify character-window splitting, exact offsets, and bounded progress."""

import re
import tempfile
import unittest

from zhiwiki.retrieval import split_chunks
from zhiwiki.store import KnowledgeStore


class ChunkingTests(unittest.TestCase):
    """Every original character must remain reconstructable after overlap removal."""

    def assert_complete_partition(self, text, size=900, overlap=120):
        """Check exact slices, forward progress, overlap, and lossless rebuilding."""
        chunks = split_chunks(text, size=size, overlap=overlap)
        if not text:
            self.assertEqual(chunks, [])
            return chunks
        self.assertTrue(chunks)
        self.assertEqual(chunks[0]["start_offset"], 0)
        self.assertEqual(chunks[-1]["end_offset"], len(text))
        rebuilt = []
        previous_start = -1
        covered_end = 0
        for index, chunk in enumerate(chunks):
            start, end = chunk["start_offset"], chunk["end_offset"]
            self.assertGreater(start, previous_start)
            self.assertGreater(end, covered_end)
            self.assertLessEqual(end, len(text))
            self.assertLessEqual(end - start, size)
            self.assertGreater(end, start)
            self.assertEqual(chunk["text"], text[start:end])
            self.assertLessEqual(start, covered_end, "A gap would drop original characters")
            if index:
                self.assertEqual(covered_end - start, overlap)
            rebuilt.append(chunk["text"][covered_end - start:])
            previous_start = start
            covered_end = end
        self.assertEqual("".join(rebuilt), text)
        return chunks

    def test_empty_text_produces_no_chunks(self):
        self.assert_complete_partition("")

    def test_short_text_keeps_leading_and_trailing_whitespace(self):
        source = "  售后资料原文。\n末行仍有空格。  \n"
        chunks = self.assert_complete_partition(source)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["text"], source)

    def test_exact_window_and_short_tail(self):
        for length in (899, 900, 901, 1680, 1681, 3001):
            with self.subTest(length=length):
                self.assert_complete_partition("甲" * length)

    def test_default_long_chinese_paragraph(self):
        chunks = self.assert_complete_partition("退款条款" * 1500)
        self.assertGreater(len(chunks), 5)
        self.assertEqual(chunks[0]["end_offset"], 900)
        self.assertEqual(chunks[1]["start_offset"], 780)

    def test_chinese_sentences_and_newlines_preserve_every_character(self):
        source = ("退货条件以签收时间为准。\n质量问题可申请换货。\n\n请保留发票和订单编号。\n" * 150)
        self.assert_complete_partition(source)

    def test_english_periods_are_not_special_boundaries(self):
        source = "English sentence. Version 2.5 remains effective. " * 80
        chunks = self.assert_complete_partition(source, size=90, overlap=20)
        self.assertEqual(chunks[0]["end_offset"], 90)

    def test_last_newline_or_chinese_period_wins_without_type_priority(self):
        sources = [
            "甲" * 10 + "\n" + "乙" * 4 + "。" + "丙" * 40,
            "甲" * 10 + "。" + "乙" * 4 + "\n" + "丙" * 40,
        ]
        for source in sources:
            with self.subTest(separator=source[15]):
                chunks = self.assert_complete_partition(source, size=20, overlap=3)
                self.assertEqual(chunks[0]["end_offset"], 16)
                self.assertEqual(chunks[0]["text"][-1], source[15])

    def test_boundary_in_first_half_is_ignored(self):
        source = "甲" * 3 + "。" + "乙" * 80
        chunks = self.assert_complete_partition(source, size=20, overlap=3)
        self.assertEqual(chunks[0]["end_offset"], 20)

    def test_internal_whitespace_windows_are_preserved(self):
        source = "第一条款。" + " " * 2500 + "\n\n\t" + "最后条款。"
        chunks = self.assert_complete_partition(source)
        self.assertTrue(any(not chunk["text"].strip() for chunk in chunks))

    def test_whitespace_only_nonempty_input_remains_reconstructable(self):
        self.assert_complete_partition(" \t\r\n" * 700)

    def test_crlf_emoji_and_combining_characters_use_python_string_offsets(self):
        source = "# 条款🙂\r\nCafé e\u0301 与退款条件。\r\n" * 100
        self.assert_complete_partition(source, size=120, overlap=20)

    def test_zero_overlap_is_an_exact_partition(self):
        self.assert_complete_partition("甲" * 13 + "。\n" + "乙" * 150, size=20, overlap=0)

    def test_one_character_windows_terminate_and_preserve_input(self):
        self.assert_complete_partition("甲\n乙🙂\t。", size=1, overlap=0)

    def test_large_valid_overlap_always_adds_new_source_characters(self):
        # A boundary shorter than overlap must not cause repeated non-growing ends.
        source = "甲" * 11 + "\n" + "乙" * 48
        self.assert_complete_partition(source, size=20, overlap=19)

    def test_invalid_text_type_is_rejected(self):
        for value in (None, 17, b"text", ["text"]):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    split_chunks(value)

    def test_invalid_size_is_rejected_even_when_text_is_empty(self):
        for value in (0, -1, 3.5, "900", True, None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    split_chunks("", size=value, overlap=0)

    def test_invalid_overlap_is_rejected(self):
        for value in (-1, 20, 21, 2.5, "3", True, None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    split_chunks("甲" * 30, size=20, overlap=value)

    def test_stored_locator_matches_exact_archived_document_slice(self):
        """A displayed one-based inclusive locator maps to the stored raw text."""
        source = "  # 售后条款🙂\r\n" + "普通商品可申请退货。\n" * 160 + "  "
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(directory)
            document = store.ingest("退货原文", source, "chunking/offsets")
            archived = store.get_document(document["id"])
            self.assertEqual(archived["text"], source)
            for chunk in archived["chunks"]:
                match = re.fullmatch(r"chars:(\d+)-(\d+)", chunk["locator"])
                self.assertIsNotNone(match)
                first, last = map(int, match.groups())
                self.assertEqual(chunk["text"], source[first - 1:last])
                self.assertEqual(chunk["document_id"], document["id"])


class StructureChunkingTests(unittest.TestCase):
    """Test opt-in structural behavior without changing default ingestion."""

    def assert_structure_rebuilds(self, text, size=100, overlap=15):
        chunks = split_chunks(text, size=size, overlap=overlap, strategy="structure")
        if not text:
            self.assertEqual(chunks, [])
            return chunks
        rebuilt = []
        previous_end = 0
        previous_start = -1
        previous_section = None
        for chunk in chunks:
            start, end = chunk["start_offset"], chunk["end_offset"]
            self.assertGreater(start, previous_start)
            self.assertGreater(end, previous_end)
            self.assertLessEqual(start, previous_end)
            self.assertLessEqual(end - start, size)
            self.assertEqual(chunk["text"], text[start:end])
            self.assertEqual(chunk["strategy"], "structure")
            self.assertLessEqual(chunk["section_start"], start)
            self.assertGreaterEqual(chunk["section_end"], end)
            self.assertIsInstance(chunk["heading_path"], list)
            actual_overlap = previous_end - start
            self.assertEqual(chunk["overlap_chars"], actual_overlap)
            self.assertLessEqual(actual_overlap, overlap)
            if chunk["section_start"] != previous_section:
                self.assertEqual(start, chunk["section_start"])
                self.assertEqual(actual_overlap, 0)
            rebuilt.append(chunk["text"][actual_overlap:])
            previous_start, previous_end = start, end
            previous_section = chunk["section_start"]
        self.assertEqual(previous_end, len(text))
        self.assertEqual("".join(rebuilt), text)
        return chunks

    def test_default_remains_the_fixed_baseline(self):
        source = "甲" * 2000
        chunks = split_chunks(source)
        self.assertEqual(chunks, split_chunks(source, strategy="fixed"))
        self.assertEqual(
            [(chunk["start_offset"], chunk["end_offset"]) for chunk in chunks],
            [(0, 900), (780, 1680), (1560, 2000)],
        )
        self.assertEqual(set(chunks[0]), {"text", "start_offset", "end_offset"})

    def test_empty_and_whitespace_structure_inputs(self):
        self.assert_structure_rebuilds("")
        self.assert_structure_rebuilds(" \r\n\t" * 120)

    def test_heading_paths_follow_levels_and_preserve_preamble(self):
        source = "前言\n# 售后\n总则\n## 退货\n七天\n### 凭证\n订单\n## 换货\n十五天\n# 运费\n另行核实\n"
        chunks = self.assert_structure_rebuilds(source)
        self.assertEqual(
            [chunk["heading_path"] for chunk in chunks],
            [[], ["售后"], ["售后", "退货"], ["售后", "退货", "凭证"],
             ["售后", "换货"], ["运费"]],
        )

    def test_paragraph_boundary_precedes_later_sentence_boundary(self):
        source = "甲" * 60 + "\n\n" + "乙" * 20 + "。" + "丙" * 100
        chunks = self.assert_structure_rebuilds(source)
        self.assertEqual(chunks[0]["end_offset"], 62)
        self.assertEqual(chunks[0]["boundary"], "paragraph")

    def test_chinese_sentence_endings_are_recognized(self):
        for punctuation in ("。", "！", "？"):
            with self.subTest(punctuation=punctuation):
                source = "甲" * 60 + punctuation + "乙" * 100
                chunks = self.assert_structure_rebuilds(source)
                self.assertEqual(chunks[0]["end_offset"], 61)
                self.assertEqual(chunks[0]["boundary"], "sentence")

    def test_english_sentence_endings_are_recognized(self):
        for punctuation in (".", "!", "?"):
            with self.subTest(punctuation=punctuation):
                source = "A" * 60 + punctuation + " " + "B" * 100
                chunks = self.assert_structure_rebuilds(source)
                self.assertEqual(chunks[0]["end_offset"], 61)
                self.assertEqual(chunks[0]["boundary"], "sentence")

    def test_decimal_point_does_not_become_a_sentence_boundary(self):
        source = "A" * 55 + " 3.14 " + "B" * 100
        chunks = self.assert_structure_rebuilds(source, size=70, overlap=5)
        self.assertEqual(chunks[0]["end_offset"], 70)
        self.assertEqual(chunks[0]["boundary"], "hard_limit")

    def test_fitting_fenced_block_is_kept_together_and_hides_fake_heading(self):
        prefix = "# Intro\n" + "A" * 30 + "\n\n"
        code = "```python\n# not a section\n" + "x=1\n" * 9 + "```\n"
        source = prefix + code + "尾注" * 80 + "\n## Real\n真实章节\n"
        chunks = self.assert_structure_rebuilds(source)
        code_start, code_end = len(prefix), len(prefix) + len(code)
        self.assertTrue(any(chunk["start_offset"] <= code_start
                            and chunk["end_offset"] >= code_end for chunk in chunks))
        self.assertTrue(any(chunk["boundary"] == "fence_start" for chunk in chunks))
        self.assertTrue(all("not a section" not in chunk["heading_path"] for chunk in chunks))
        self.assertEqual(chunks[-1]["heading_path"], ["Intro", "Real"])

    def test_long_fenced_block_has_bounded_continuation(self):
        source = "# Long\n```text\n" + "# pretend heading\n" * 40 + "```\n## End\n完成"
        chunks = self.assert_structure_rebuilds(source)
        self.assertTrue(any(chunk["boundary"] == "hard_fence" for chunk in chunks))
        self.assertTrue(all("pretend heading" not in chunk["heading_path"] for chunk in chunks))

    def test_unclosed_fence_protects_headings_until_end_of_document(self):
        source = "# Policy\n~~~text\n## fake\n" + "sample line\n" * 40
        chunks = self.assert_structure_rebuilds(source)
        self.assertTrue(all(chunk["heading_path"] == ["Policy"] for chunk in chunks))

    def test_fence_closing_marker_must_match_opening_marker(self):
        source = "# Root\n~~~~\n## fake\n```\n## still fake\n~~~~\n## Real\nFinal text"
        chunks = self.assert_structure_rebuilds(source)
        self.assertEqual([chunk["heading_path"] for chunk in chunks], [["Root"], ["Root", "Real"]])

    def test_overlap_never_crosses_heading_sections(self):
        source = "# One\n" + "甲" * 350 + "\n# Two\n" + "乙" * 350
        chunks = self.assert_structure_rebuilds(source)
        second_start = source.index("# Two")
        before = [chunk for chunk in chunks if chunk["heading_path"] == ["One"]]
        after = [chunk for chunk in chunks if chunk["heading_path"] == ["Two"]]
        self.assertEqual(before[-1]["end_offset"], second_start)
        self.assertEqual(after[0]["start_offset"], second_start)
        self.assertEqual(after[0]["overlap_chars"], 0)
        self.assertTrue(any(chunk["overlap_chars"] == 15 for chunk in before[1:]))

    def test_structure_handles_high_overlap_and_single_character_windows(self):
        self.assert_structure_rebuilds("甲" * 11 + "。\n" + "乙" * 60, size=20, overlap=19)
        self.assert_structure_rebuilds("甲\n乙🙂\t。", size=1, overlap=0)

    def test_unknown_strategy_is_rejected(self):
        for strategy in ("semantic", "", None, [], True):
            with self.subTest(strategy=repr(strategy)):
                with self.assertRaises(ValueError):
                    split_chunks("sample", strategy=strategy)


if __name__ == "__main__":
    unittest.main()
