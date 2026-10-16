"""Immutable sparse BM25 contributions: SQLite persistence, NumPy scoring.

Term postings are derived data, never executable pickle. Query arrays are local
to a request; no shared mutable score buffer or permission-dependent result cache.
"""

import hashlib
import json
import math
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from contextlib import closing

import numpy as np

from ..retrieval import _anchor_terms, tokenize
from .common import ServiceError, check_deadline, encode, uid


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class PersistentIndex:
    def __init__(self, path=None):
        self.path = path

    def validate(self):
        if self.path is None:
            return
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as connection:
            row = connection.execute("SELECT version,count FROM metadata").fetchone()
            if (not row or row[0] != 'sparse-bm25-v1' or not 0 <= row[1] <= 50000
                    or connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok'
                    or connection.execute('SELECT COUNT(*) FROM items').fetchone()[0] != row[1]):
                raise ServiceError(503, 'INDEX_NOT_READY')

    @classmethod
    def build(cls, database, candidates):
        temporary = database.root / "indexes" / (uid("build") + ".sqlite3")
        connection = sqlite3.connect(str(temporary))
        try:
            cls.populate(connection, candidates)
            connection.commit()
        finally:
            connection.close()
        if temporary.stat().st_size > 512 * 1024 * 1024:
            temporary.unlink()
            raise ServiceError(422, "INDEX_TOO_LARGE")
        target = temporary.with_name(checksum(temporary) + ".sqlite3")
        if target.exists():
            temporary.unlink()
        else:
            temporary.replace(target)
        return str(target.relative_to(database.root)).replace("\\", "/"), cls(target)

    @staticmethod
    def populate(connection, candidates):
        connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY,evidence_id TEXT UNIQUE,book_id TEXT,payload TEXT)")
        connection.execute("CREATE INDEX items_by_book ON items(book_id)")
        connection.execute("CREATE TABLE postings(term TEXT PRIMARY KEY,ids BLOB,weights BLOB)")
        connection.execute("CREATE TABLE metadata(version TEXT,count INTEGER)")
        count = len(candidates)
        connection.execute("INSERT INTO metadata VALUES('sparse-bm25-v1',?)", (count,))
        postings, lengths = defaultdict(list), []
        for ordinal, item in enumerate(sorted(candidates, key=lambda value: value["id"])):
            terms = Counter(tokenize(item["text"]))
            # A title match has twice the term frequency of a body match.
            terms.update({term: frequency * 2 for term, frequency in Counter(tokenize(item["title"])).items()})
            lengths.append(sum(terms.values()))
            for term, frequency in terms.items():
                postings[term].append((ordinal, frequency))
            connection.execute("INSERT INTO items VALUES(?,?,?,?)", (ordinal, item["id"], item["document_id"], encode(item)))
        average = sum(lengths) / max(1, count)
        for term, values in postings.items():
            ids = np.fromiter((value[0] for value in values), dtype="<u4")
            frequency = np.fromiter((value[1] for value in values), dtype="<f4")
            document_lengths = np.fromiter((lengths[value[0]] for value in values), dtype="<f4")
            inverse_frequency = math.log(1 + (count - len(values) + .5) / (len(values) + .5))
            weights = inverse_frequency * frequency * 2.5 / (frequency + 1.5 * (.25 + .75 * document_lengths / max(1, average)))
            connection.execute("INSERT INTO postings VALUES(?,?,?)", (term, ids.tobytes(), weights.astype("<f4").tobytes()))

    def search(self, query, limit, deadline, allowed_documents=None):
        check_deadline(deadline)
        if self.path is None or allowed_documents == set():
            return []
        query = unicodedata.normalize('NFKC', query)
        terms = sorted(set(tokenize(query)))
        anchors = _anchor_terms(query)
        if not terms or not anchors:
            return []
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=.1)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA mmap_size=268435456")
            connection.set_progress_handler(lambda: int(__import__("time").monotonic() >= deadline), 1000)
            count = connection.execute("SELECT count FROM metadata WHERE version='sparse-bm25-v1'").fetchone()[0]
            scores = np.zeros(count, dtype=np.float32)
            anchored = np.zeros(count, dtype=bool)
            for term in terms:
                check_deadline(deadline)
                row = connection.execute("SELECT ids,weights FROM postings WHERE term=?", (term,)).fetchone()
                if row:
                    ids = np.frombuffer(row[0], dtype="<u4")
                    scores[ids] += np.frombuffer(row[1], dtype="<f4")
                    if term in anchors:
                        anchored[ids] = True
            scores[~anchored] = 0
            if allowed_documents is not None:
                # Apply metadata/authorization filters before top-k selection.
                mask = np.zeros(count, dtype=bool)
                for book in sorted(allowed_documents):
                    rows = connection.execute("SELECT id FROM items WHERE book_id=?", (book,))
                    mask[np.fromiter((row[0] for row in rows), dtype=np.intp)] = True
                scores[~mask] = 0
            selected = np.flatnonzero(scores > 0)
            ordered = selected[np.lexsort((selected, -scores[selected]))[:limit]]
            result = []
            for ordinal in ordered:
                payload = connection.execute("SELECT payload FROM items WHERE id=?", (int(ordinal),)).fetchone()[0]
                result.append(dict(json.loads(payload), score=float(scores[ordinal])))
            check_deadline(deadline)
            return result
        except sqlite3.OperationalError:
            check_deadline(deadline)
            raise ServiceError(503, "INDEX_NOT_READY") from None
        finally:
            connection.close()
