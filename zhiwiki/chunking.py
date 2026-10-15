"""Dependency-free character and structure splitters with exact source offsets.

Design references are recorded in docs/STRATEGY_SOURCES.md. This is a local
implementation of structural principles, not a semantic or token-based splitter.
The 900/120 defaults are unoptimized character baselines, not vendor guidance.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
PARAGRAPH_END = re.compile(r"\r?\n[ \t]*\r?\n")
# Decimal points do not act as sentence ends. Abbreviations remain heuristic.
SENTENCE_END = re.compile(
    r"[。！？]+[”’」』）)\]]*|[!?]+[\"')\]]*(?=\s|$)|(?<!\d)\.[\"')\]]*(?=\s|$)"
)


@dataclass(frozen=True)
class Section:
    start: int
    end: int
    heading_path: Tuple[str, ...]


def split_chunks(text: str, size: int = 900, overlap: int = 120,
                 strategy: str = "fixed") -> List[Dict[str, Any]]:
    """Split using the existing fixed baseline or an opt-in structure strategy."""
    _validate(text, size, overlap, strategy)
    if strategy == "fixed":
        return _fixed_chunks(text, size, overlap)
    return _structure_chunks(text, size, overlap)


def _validate(text, size, overlap, strategy):
    """Reject invalid parameters before processing even an empty document."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if type(size) is not int or size < 1:
        raise ValueError("size must be a positive integer")
    if type(overlap) is not int or not 0 <= overlap < size:
        raise ValueError("overlap must be an integer satisfying 0 <= overlap < size")
    if not isinstance(strategy, str) or strategy not in ("fixed", "structure"):
        raise ValueError("strategy must be fixed or structure")


def _fragment(text, start, end):
    return {"text": text[start:end], "start_offset": start, "end_offset": end}


def _fixed_chunks(text, size, overlap):
    """Preserve the previous algorithm and return shape for default ingestion."""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            lower = start + max(size // 2, overlap)
            boundary = max(text.rfind("\n", lower, end), text.rfind("。", lower, end))
            if boundary >= lower:
                end = boundary + 1
        chunks.append(_fragment(text, start, end))
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def _heading(line):
    match = HEADING.fullmatch(line.rstrip("\r\n"))
    if match is None:
        return None
    title = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2) or "").strip()
    return len(match.group(1)), title


def _scan_structure(text):
    """Identify ATX sections and fences without normalizing any source text."""
    sections = []
    fences = []
    headings = []
    offset = section_start = 0
    open_fence = None
    for line in text.splitlines(keepends=True):
        marker = FENCE.fullmatch(line.rstrip("\r\n"))
        if open_fence is not None:
            if (marker and marker.group(1)[0] == open_fence[0]
                    and len(marker.group(1)) >= open_fence[1]
                    and not marker.group(2).strip()):
                fences.append((open_fence[2], offset + len(line)))
                open_fence = None
        elif marker:
            open_fence = (marker.group(1)[0], len(marker.group(1)), offset)
        else:
            heading = _heading(line)
            if heading is not None:
                if offset > section_start:
                    sections.append(Section(section_start, offset, tuple(item[1] for item in headings)))
                while headings and headings[-1][0] >= heading[0]:
                    headings.pop()
                headings.append(heading)
                section_start = offset
        offset += len(line)
    if section_start < len(text):
        sections.append(Section(section_start, len(text), tuple(item[1] for item in headings)))
    if open_fence is not None:
        fences.append((open_fence[2], len(text)))
    return sections, fences


def _containing_fence(position, fences):
    """An endpoint at a fence boundary is safe; only its interior is protected."""
    for start, end in fences:
        if start < position < end:
            return start, end
    return None


def _boundary_positions(pattern, text, section, fences):
    """Keep absolute separator endpoints outside fenced code."""
    return [match.end() for match in pattern.finditer(text, section.start, section.end)
            if not _containing_fence(match.end(), fences)]


def _last_boundary(positions, lower, upper):
    index = bisect_right(positions, upper) - 1
    if index >= 0 and positions[index] > lower:
        return positions[index]
    return None


def _choose_end(start, size, overlap, section, paragraphs, sentences, fences):
    """Prefer paragraph then sentence ends; hard caps always remain enforceable."""
    limit = min(start + size, section.end)
    if limit == section.end:
        return limit, "section_end"
    fence = _containing_fence(limit, fences)
    if fence and fence[0] > start:
        # Start the next chunk at the fence instead of splitting a fitting block.
        return fence[0], "fence_start"
    lower = start + max(size // 2, overlap)
    for positions, reason in ((paragraphs, "paragraph"), (sentences, "sentence")):
        boundary = _last_boundary(positions, lower, limit)
        if boundary is not None:
            return boundary, reason
    return limit, "hard_fence" if fence else "hard_limit"


def _next_start(start, end, overlap, reason, fences):
    """Avoid copying an incomplete tail of a finished fenced code block."""
    if reason == "fence_start":
        return end
    following = max(start + 1, end - overlap)
    fence = _containing_fence(following, fences)
    if fence and end >= fence[1]:
        return end
    return following


def _split_section(text, section, fences, size, overlap):
    paragraphs = _boundary_positions(PARAGRAPH_END, text, section, fences)
    sentences = _boundary_positions(SENTENCE_END, text, section, fences)
    chunks = []
    start = previous_end = section.start
    while start < section.end:
        end, reason = _choose_end(start, size, overlap, section, paragraphs, sentences, fences)
        chunk = _fragment(text, start, end)
        chunk.update({
            "strategy": "structure", "heading_path": list(section.heading_path),
            "section_start": section.start, "section_end": section.end,
            "boundary": reason, "overlap_chars": max(0, previous_end - start),
        })
        chunks.append(chunk)
        if end == section.end:
            break
        previous_end = end
        start = _next_start(start, end, overlap, reason, fences)
    return chunks


def _structure_chunks(text, size, overlap):
    sections, fences = _scan_structure(text)
    chunks = []
    for section in sections:
        section_fences = [(start, end) for start, end in fences
                          if start >= section.start and end <= section.end]
        chunks.extend(_split_section(text, section, section_fences, size, overlap))
    return chunks
