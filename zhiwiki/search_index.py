"""One immutable lexical snapshot per store, invalidated by a database epoch."""

from threading import RLock
from time import perf_counter

from .retrieval import BM25Index


class SearchIndexCache:
    """The epoch read and corpus load must use the caller's read transaction."""

    def __init__(self):
        self._lock = RLock()
        self._index = None
        self._epoch = None
        self._hits = self._misses = self._builds = 0
        self._last_build_ms = 0.0

    def get(self, connection, load_candidates):
        # Serialize cold builds in one store. The first SELECT pins SQLite's
        # snapshot, so a writer cannot pair an old epoch with a newer corpus.
        with self._lock:
            epoch = connection.execute("SELECT epoch FROM retrieval_state WHERE id=1").fetchone()[0]
            if self._index is not None and epoch == self._epoch:
                self._hits += 1
                return self._index
            self._misses += 1
            began = perf_counter()
            index = BM25Index(load_candidates(connection))
            self._builds += 1
            self._last_build_ms = round((perf_counter() - began) * 1000, 3)
            # An older transaction may finish after a newer one; it can use its
            # own snapshot but must not replace a newer cached generation.
            if self._epoch is None or epoch >= self._epoch:
                self._index, self._epoch = index, epoch
            return index

    def stats(self):
        """Return aggregate counters only: no text, queries, paths, or secrets."""
        with self._lock:
            return {"ready": self._index is not None, "epoch": self._epoch,
                    "hits": self._hits, "misses": self._misses, "builds": self._builds,
                    "items": len(self._index.items) if self._index is not None else 0,
                    "terms": len(self._index.postings) if self._index is not None else 0,
                    "last_build_ms": self._last_build_ms}
