"""Small-corpus BM25 retrieval; Chinese tokens are characters and bigrams.

This module deliberately does not claim semantic or embedding retrieval. A
prepared index reuses token counts; its owner controls SQLite snapshot freshness.
"""

import math
import heapq
import re
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any, Dict, Iterable, List


TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:['_-][a-z0-9]+)*|[\u3400-\u4dbf\u4e00-\u9fff]+", re.I)

# These are query/function words, not corpus-specific answer keywords. They may
# contribute to BM25 ranking but cannot be the only reason a result is returned.
ENGLISH_QUERY_WORDS = frozenset("""
a an the and or but of to for from in on at by with about as is are was were be
been being do does did can could will would shall should may might must have has
had i me my we our you your he she it its they their this that these those what
which who whom whose when where why how please tell explain describe define
definition meaning information know give show there here any some all not
""".split())
CHINESE_QUERY_WORDS = frozenset("""
什么 为什么 怎么 怎么样 如何 哪些 哪个 哪里 在哪里 多少 几个 是否 能否 可以 可否
是什么 什么是 是什么样 的含义 的定义 的意思 的说明
能不能 有没有 没有 有何 何为 为何 定义 含义 意思 解释 说明 介绍 描述
告诉 告诉我 我想知道 想知道 知道 理解 请问 请给 请告 一下 一些 一个 这个 那个 这样 那样 这些
那些 关于 有关 相关 需要 应该 应当 能够 帮我 帮忙 请帮 请您 请你 的话
""".split())
CHINESE_GLUE_CHARACTERS = frozenset("的了呢吗吧啊呀是与及和或而这那么您我你他她它被把也就都很该其")
CHINESE_QUERY_PATTERN = re.compile("|".join(sorted(CHINESE_QUERY_WORDS, key=lambda word: (-len(word), word))))


def tokenize(text: str) -> List[str]:
    """Keep English words and Chinese unigrams/bigrams without dependencies."""
    tokens = []
    for match in TOKEN_PATTERN.finditer(text.lower()):
        term = match.group(0)
        if "\u3400" <= term[0] <= "\u9fff":
            tokens.extend(term)
            tokens.extend(term[index:index + 2] for index in range(len(term) - 1))
        else:
            tokens.append(term)
    return tokens


def _anchor_terms(query: str):
    """Require a lexical content match; this is not a semantic answerability test.

    Chinese unigrams help BM25 ranking but are too broad to anchor a multiword
    question. A user explicitly searching one character retains that behavior.
    """
    surface_terms = [match.group(0).lower() for match in TOKEN_PATTERN.finditer(query)]
    if (len(surface_terms) == 1 and len(surface_terms[0]) == 1
            and surface_terms[0] not in CHINESE_GLUE_CHARACTERS
            and surface_terms[0] not in ENGLISH_QUERY_WORDS):
        return set(surface_terms)
    anchors = set()
    # Remove whole question phrases before producing bigrams. Otherwise a
    # meaningless boundary token such as “以告” in “可以告诉” can survive.
    content_query = CHINESE_QUERY_PATTERN.sub(" ", query)
    for match in TOKEN_PATTERN.finditer(content_query.lower()):
        surface = match.group(0)
        if "\u3400" <= surface[0] <= "\u9fff":
            # Do not trim a single function character from a content term: it
            # may be a real compound's suffix. Remove whole query frames above.
            # A bigram is generic only if *both* characters are glue.
            for term in tokenize(surface):
                if (len(term) == 2 and term not in CHINESE_QUERY_WORDS
                        and not set(term).issubset(CHINESE_GLUE_CHARACTERS)):
                    anchors.add(term)
        elif surface not in ENGLISH_QUERY_WORDS:
            anchors.add(surface)
    return anchors


def bm25_search(query: str, evidence: Iterable[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    """Compatibility path for one-shot callers without a persistent cache."""
    return BM25Index(evidence).search(query, limit=limit)


class BM25Index:
    """Prepared lexical index; search results never expose cached mutable data."""

    def __init__(self, evidence):
        self.items = tuple(deepcopy(item) for item in evidence if item["text"].strip())
        self.counters = tuple(Counter(tokenize(item["title"] + " " + item["title"] + " " + item["text"]))
                              for item in self.items)
        self.lengths = tuple(sum(counts.values()) for counts in self.counters)
        self.postings = defaultdict(set)
        for index, counts in enumerate(self.counters):
            for term in counts:
                self.postings[term].add(index)
        self.views = {kind: self._view(kind) for kind in ("all", "document", "wiki")}

    def _view(self, kind):
        indices = {index for index, item in enumerate(self.items)
                   if kind == "all" or item.get("kind") == kind}
        frequencies = Counter()
        for index in indices:
            frequencies.update(self.counters[index].keys())
        average = sum(self.lengths[index] for index in indices) / max(len(indices), 1) or 1.0
        return indices, frequencies, average

    def search(self, query, limit=5, kind="all", check_cancel=None):
        """BM25 k1=1.5, b=0.75; count/title weighting is unchanged from v2."""
        terms = set(tokenize(query))
        anchors = _anchor_terms(query)
        if not terms or not anchors:
            return []
        indices, frequencies, average = self.views[kind]
        candidates = set()
        for term in anchors:
            candidates.update(self.postings.get(term, ()))
        ranked = []
        for position, index in enumerate(candidates.intersection(indices)):
            if check_cancel is not None and position % 128 == 0:
                check_cancel()
            score = _bm25_score(terms, self.counters[index], self.lengths[index],
                                average, frequencies, len(indices))
            if score > 0:
                ranked.append((round(score, 8), index))
        # Copy only selected results: high-frequency terms must not duplicate
        # the entire corpus just to discard all but top-k afterwards.
        selected = heapq.nsmallest(limit, ranked, key=lambda item: (-item[0], self.items[item[1]]["id"]))
        return [dict(deepcopy(self.items[index]), score=score) for score, index in selected]


def _bm25_score(terms, counts, length, average_length, frequencies, document_count):
    """Use positive IDF so frequent terms never introduce negative ranking."""
    total = 0.0
    for term in terms:
        count = counts.get(term, 0)
        if not count:
            continue
        frequency = frequencies[term]
        inverse_frequency = math.log(1.0 + (document_count - frequency + 0.5) / (frequency + 0.5))
        denominator = count + 1.5 * (1.0 - 0.75 + 0.75 * length / average_length)
        total += inverse_frequency * count * 2.5 / denominator
    return total


def split_chunks(text: str, size: int = 900, overlap: int = 120,
                 strategy: str = "fixed") -> List[Dict[str, Any]]:
    """Compatibility entry point; default ingestion keeps the fixed baseline."""
    from .chunking import split_chunks as split_source

    return split_source(text, size=size, overlap=overlap, strategy=strategy)
