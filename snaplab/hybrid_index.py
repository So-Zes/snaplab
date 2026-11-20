"""The default recall index: word search over chunks plus an optional search by meaning, fused (round 21).

HybridIndex extends FtsIndex: the records/docs/replacements tables, Scope and the time rules stay as they are, and
FtsIndex.search is still used while an older index is being upgraded. On top it keeps:

1. Chunks. Each record's text is cut into windows of about 350 tokens (estimate: 0.75 per CJK character, 0.45 per
   other character), ending at a line break or sentence end in the last 30% of the window, with 40 tokens of overlap.
   chunks(cid, rec, ord, start, stop, ts, vec): offsets into the record's text, so no text is stored twice.
2. Two word-search legs over the chunks (NFKC + lower case), contentless FTS5 tables:
     ctri  trigram tokenizer: OR of the query's terms of 3+ characters (space-split, plus the pieces between Japanese
           particles)
     cbi   CJK character bigrams (+ the last character of each run) and ASCII words; a 1-character word is a prefix
           query. This finds 1-2 character Japanese words, which trigrams cannot.
   Each leg ranks by bm25(text 1.0, title 0.5), top 200 chunks; the legs are fused by RRF (k=60), newer first on ties.
3. A meaning leg (optional, see embedder.py): multilingual-e5-small vectors of each record's first 16 chunks (the
   title goes before the first one), kept as float16 BLOBs in chunks.vec and as one float32 matrix in RAM; brute-force
   cosine, top 200 chunks. A background thread makes the vectors (2 threads, pauses while a chat turn runs), so
   neither startup nor a chat reply waits for it.
4. Per record: score = 1/(60 + lexical rank) + 0.5/(60 + meaning rank) + 0.05 * coverage, where coverage is the share
   of the query's terms (space-separated words, or for a sentence without spaces the pieces between particles) found
   in the record's best chunk and title. A record found only by the word legs must contain at least one query term,
   unless the query is a sentence. Newer first on ties.
Each hit also carries coverage (0..1), dense (cosine of its best chunk, or None) and matched (the number of terms).

Upgrading an older index.sqlite3: the chunk tables are added and the old records are chunked by the background
thread (progress in hybrid_meta, so a restart resumes); until that is finished, search() is FtsIndex.search.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
import unicodedata
from bisect import bisect_left, bisect_right
from itertools import accumulate
from pathlib import Path

from .archive import FtsIndex, Scope

W_CJK, W_OTHER = 0.75, 0.45
MAX_TOKENS, OVERLAP_TOKENS, EMBED_CHUNKS = 350, 40, 16
RRF_K, W_LEX, W_DENSE, GAMMA = 60, 1.0, 0.5, 0.05
LEG_CHUNKS, LEG_RECORDS = 200, 100
INLINE_CHARS = 200_000            # bigger records are chunked by the background thread
BACKFILL_BATCH, EMBED_BATCH = 200, 16
CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]")
RUN = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]+|[a-z0-9_]+")
HIRA = re.compile(r"^[぀-ゟ]+$")
SPACES = re.compile(r"[\s,、。・/|]+")
PIECE_SPLIT = re.compile(r"[\s,、。・/|?？!！「」『』()（）\[\]{}\"':：]+|[のをはがにでとへもや]")


def norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def spans(text: str, max_tokens: float = MAX_TOKENS, overlap: float = OVERLAP_TOKENS) -> list[tuple[int, int]]:
    """Chunk windows (start, stop) of a text."""
    n = len(text)
    if not n:
        return []
    pre = list(accumulate((W_CJK if CJK.match(c) else W_OTHER for c in text), initial=0.0))
    if pre[-1] <= max_tokens:
        return [(0, n)]
    out, start = [], 0
    while start < n:
        end = min(n, max(start + 1, bisect_right(pre, pre[start] + max_tokens) - 1))
        if end < n:
            lo = start + int((end - start) * 0.7)
            cut = max(text.rfind("\n", lo, end), text.rfind("。", lo, end), text.rfind(". ", lo, end))
            if cut > lo:
                end = cut + 1
        out.append((start, end))
        if end >= n:
            break
        start = max(bisect_left(pre, pre[end] - overlap), start + 1)
    return out


def bigram_tokens(text: str) -> list[str]:
    out = []
    for m in RUN.finditer(text):
        w = m.group(0)
        if w[0] < "　" or len(w) == 1:
            out.append(w)
        else:
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
            out.append(w[-1])
    return out


def _quote(t: str) -> str:
    return '"' + t.replace('"', '""') + '"'


def bigram_query(q: str) -> str:
    toks, seen = [], set()
    for m in RUN.finditer(norm(q)):
        w = m.group(0)
        cand = [(w, False)] if w[0] < "　" else [(w, True)] if len(w) == 1 else \
            [(w[i:i + 2], False) for i in range(len(w) - 1)]
        for t, pref in cand:
            if t not in seen:
                seen.add(t)
                toks.append((t, pref))
    toks = [x for x in toks if not HIRA.match(x[0])] or toks
    return " OR ".join(_quote(t) + ("*" if p else "") for t, p in toks[:64])


def trigram_query(q: str) -> str:
    qn, terms = norm(q), []
    for t in SPACES.split(qn):
        t = t.strip("\"'()[]{}?!？！「」『』")
        if t:
            terms.append(t)
    for p in PIECE_SPLIT.split(qn):
        if p and p not in terms:
            terms.append(p)
    return " OR ".join(_quote(t) for t in [t for t in terms if len(t) >= 3][:16])


def match_terms(q: str) -> list[str]:
    """The query's terms for coverage: space-separated words, or for a sentence without spaces the pieces between
    particles."""
    return query_terms(q)[0]


def query_terms(q: str) -> tuple[list[str], bool]:
    """match_terms, and whether the query is such a sentence."""
    qn, out = norm(q), []
    for t in SPACES.split(qn):
        t = t.strip("\"'()[]{}?!？！「」『』")
        if t and t not in out:
            out.append(t)
    sentence = False
    if len(out) <= 1:
        for p in PIECE_SPLIT.split(qn):
            if p and len(p) >= 2 and p not in out:
                out.append(p)
                sentence = True
    return out[:12], sentence and len(out) > 1


def rrf(rankings, k=RRF_K, weights=None):
    score = {}
    for i, ranking in enumerate(rankings):
        w = 1.0 if weights is None else weights[i]
        for r, item in enumerate(ranking):
            score[item] = score.get(item, 0.0) + w / (k + r + 1)
    return sorted(score, key=lambda x: -score[x]), score


class _Vectors:
    """Growable float32 matrix of chunk vectors (rows) with their chunk ids."""

    def __init__(self, np, dim):
        self.np, self.dim = np, dim
        self.buf = np.zeros((1024, dim), np.float32)
        self.ids = np.zeros(1024, np.int64)
        self.n = 0
        self.lock = threading.Lock()

    def add(self, ids, rows):
        np = self.np
        with self.lock:
            need = self.n + len(ids)
            if need > len(self.buf):
                cap = max(need, len(self.buf) * 2)
                buf = np.zeros((cap, self.dim), np.float32)
                buf[:self.n] = self.buf[:self.n]
                idv = np.zeros(cap, np.int64)
                idv[:self.n] = self.ids[:self.n]
                self.buf, self.ids = buf, idv
            self.buf[self.n:need] = rows
            self.ids[self.n:need] = ids
            self.n = need

    def top(self, q, k):
        np = self.np
        with self.lock:
            n = self.n
            if not n:
                return []
            s = self.buf[:n] @ q
            ids = self.ids[:n].copy()
        k = min(k, n)
        idx = np.argpartition(-s, k - 1)[:k]
        idx = idx[np.argsort(-s[idx])]
        return [(int(ids[i]), float(s[i])) for i in idx]

    def rows_for(self, cids):
        """Positions of the given chunk ids (for cosine of a known record)."""
        np = self.np
        with self.lock:
            if not self.n:
                return None, []
            mask = np.isin(self.ids[:self.n], np.array(list(cids), np.int64))
            return self.buf[:self.n][mask], list(self.ids[:self.n][mask])


class HybridIndex(FtsIndex):
    def __init__(self, path: Path, log=None, busy=None):
        super().__init__(path)
        self.log = log or (lambda line: None)
        self.busy = busy or (lambda: False)
        self.hybrid = self.fts
        self.embedder = None
        self.vectors = None
        self.semantic = "off"            # off | unavailable | loading | indexing | ready
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._defer = False
        self._pending = 0
        if not self.hybrid:
            return
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS chunks (cid INTEGER PRIMARY KEY, rec INTEGER, ord INTEGER, "
                            "start INTEGER, stop INTEGER, ts TEXT, vec BLOB)")
            self.db.execute("CREATE INDEX IF NOT EXISTS chunks_rec ON chunks(rec)")
            self.db.execute(f"CREATE INDEX IF NOT EXISTS chunks_pending ON chunks(cid) WHERE vec IS NULL "
                            f"AND ord < {EMBED_CHUNKS}")
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS ctri USING fts5(text, title, tokenize='trigram', "
                            "content='')")
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS cbi USING fts5(text, title, "
                            "tokenize='unicode61 remove_diacritics 0', content='')")
            self.db.execute("CREATE TABLE IF NOT EXISTS chunk_queue (n INTEGER PRIMARY KEY)")
            self.db.execute("CREATE TABLE IF NOT EXISTS hybrid_meta (k TEXT PRIMARY KEY, v TEXT)")
            if self._meta("chunk_target") is None:
                top = self.db.execute("SELECT COALESCE(MAX(n), 0) FROM records").fetchone()[0]
                self._set_meta("chunk_target", top)
                self._set_meta("chunk_done", 0)
            self.db.commit()
            self._target = int(self._meta("chunk_target"))
            self._done = int(self._meta("chunk_done"))

    # ---------------------------------------------------------------- bookkeeping
    def _meta(self, k):
        row = self.db.execute("SELECT v FROM hybrid_meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO hybrid_meta (k, v) VALUES (?,?)", (k, str(v)))

    def chunks_ready(self) -> bool:
        return self.hybrid and not self._defer and self._done >= self._target

    def status(self) -> dict:
        """For Settings / stats: how far the upgrade and the semantic vectors are."""
        out = {"semantic": self.semantic, "chunks_done": 1.0}
        if not self.hybrid:
            return {"semantic": "off", "chunks_done": 0.0}
        if self._target:
            out["chunks_done"] = min(1.0, self._done / self._target)
        if self.semantic in ("indexing", "ready"):
            with self.lock:
                total = self.db.execute(f"SELECT COUNT(*) FROM chunks WHERE ord < {EMBED_CHUNKS}").fetchone()[0]
            pending = self._pending
            out["vectors_done"] = 1.0 if not total else max(0.0, 1.0 - pending / total)
        return out

    def reset(self) -> None:
        """Called with the records when the whole index is rebuilt from the archive files."""
        if not self.hybrid:
            return
        with self.lock:
            self.db.execute("DELETE FROM chunks")
            self.db.execute("DELETE FROM chunk_queue")
            self.db.execute("INSERT INTO ctri(ctri) VALUES('delete-all')")
            self.db.execute("INSERT INTO cbi(cbi) VALUES('delete-all')")
            self._set_meta("chunk_target", 0)
            self._set_meta("chunk_done", 0)
            self.db.commit()
            self._target = self._done = 0
        if self.vectors is not None:
            self.vectors = _Vectors(self.vectors.np, self.vectors.dim)

    def defer_chunking(self, on: bool) -> None:
        """During a full rebuild from the files: records are written now and chunked by the background thread."""
        if not self.hybrid:
            return
        self._defer = on
        if not on:
            with self.lock:
                top = self.db.execute("SELECT COALESCE(MAX(n), 0) FROM records").fetchone()[0]
                self._target = max(self._target, top)
                self._set_meta("chunk_target", self._target)
                self.db.commit()
            self._wake.set()

    # ---------------------------------------------------------------- writing
    def _chunk_rows(self, n, text, title, ts):
        """Compute (outside the lock) what _insert_chunks writes."""
        nt = norm(title)
        bt = " ".join(bigram_tokens(nt))
        out = []
        for i, (a, b) in enumerate(spans(text)):
            piece = norm(text[a:b])
            out.append((n, i, a, b, ts or "", piece, nt, " ".join(bigram_tokens(piece)), bt))
        return out

    def _insert_chunks(self, rows):
        for n, i, a, b, ts, piece, nt, bp, bt in rows:
            cid = self.db.execute("INSERT INTO chunks (rec, ord, start, stop, ts) VALUES (?,?,?,?,?)",
                                  (n, i, a, b, ts)).lastrowid
            self.db.execute("INSERT INTO ctri(rowid, text, title) VALUES (?,?,?)", (cid, piece, nt))
            self.db.execute("INSERT INTO cbi(rowid, text, title) VALUES (?,?,?)", (cid, bp, bt))

    def add(self, rec: dict) -> None:
        if not self.hybrid:
            return super().add(rec)
        with self.lock:
            new = self.db.execute("SELECT 1 FROM records WHERE id=?", (rec["id"],)).fetchone() is None
            super().add(rec)
            if not new or self._defer:
                return
            n, title = self.db.execute("SELECT r.n, d.title FROM records r JOIN docs d ON d.rowid = r.n WHERE r.id=?",
                                       (rec["id"],)).fetchone()
            if n <= self._target:                     # an old record: the backfill does it
                return
            text = rec.get("text") or ""
            if len(text) > INLINE_CHARS:
                self.db.execute("INSERT OR IGNORE INTO chunk_queue (n) VALUES (?)", (n,))
            else:                                     # title: FtsIndex's search title (with entity names and aliases)
                self._insert_chunks(self._chunk_rows(n, text, title or "", rec.get("ts")))
            self.db.commit()
        self._wake.set()

    # ---------------------------------------------------------------- background work
    def start(self, embedder_factory=None) -> None:
        """Start the background thread: finish the chunk backfill, then (when embedder_factory gives a model) make
        the vectors. embedder_factory runs in the thread (it may download the model) and returns a model or None."""
        if not self.hybrid or self._thread:
            return
        self._factory = embedder_factory
        self.semantic = "loading" if embedder_factory else "off"
        self._thread = threading.Thread(target=self._run, name="recall-index", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        super().close()

    def _run(self):
        try:
            self._backfill()
            if self._stop.is_set():
                return
            if self._factory:
                model = None
                try:
                    model = self._factory()
                except Exception as e:  # noqa: BLE001
                    self.log(f"semantic search off: {e!r}")
                if model is None:
                    self.semantic = "unavailable"
                else:
                    self._attach(model)
            while not self._stop.is_set():
                worked = self._drain_queue() or self._backfill()
                if self.embedder is not None:
                    if self.busy():
                        self._stop.wait(1.0)
                        continue
                    worked = self._embed_batch() or worked
                if not worked:
                    self._wake.wait(30.0)
                    self._wake.clear()
        except (sqlite3.ProgrammingError, sqlite3.OperationalError) as e:
            if not self._stop.is_set():
                self.log(f"recall index background work stopped: {e!r}")
        except Exception as e:  # noqa: BLE001 - never take the app down
            self.log(f"recall index background work stopped: {e!r}")

    def _backfill(self) -> bool:
        did = False
        while self._done < self._target and not self._stop.is_set():
            with self.lock:
                rows = self.db.execute("SELECT r.n, r.ts, d.title, d.text FROM records r JOIN docs d ON d.rowid = r.n "
                                       "WHERE r.n > ? AND r.n <= ? ORDER BY r.n LIMIT ?",
                                       (self._done, self._target, BACKFILL_BATCH)).fetchall()
            batch = [self._chunk_rows(n, text or "", title or "", ts) for n, ts, title, text in rows]
            with self.lock:
                if self._stop.is_set():
                    return did
                for b in batch:
                    self._insert_chunks(b)
                self._done = rows[-1][0] if rows else self._target
                self._set_meta("chunk_done", self._done)
                self.db.commit()
            did = True
            time.sleep(0.005)
        return did

    def _drain_queue(self) -> bool:
        with self.lock:
            row = self.db.execute("SELECT q.n, r.ts, d.title, d.text FROM chunk_queue q JOIN records r ON r.n = q.n "
                                  "JOIN docs d ON d.rowid = q.n ORDER BY q.n LIMIT 1").fetchone()
            if not row:
                return False
        rows = self._chunk_rows(row[0], row[3] or "", row[2] or "", row[1])
        with self.lock:
            if not self.db.execute("SELECT 1 FROM chunks WHERE rec=? LIMIT 1", (row[0],)).fetchone():
                self._insert_chunks(rows)
            self.db.execute("DELETE FROM chunk_queue WHERE n=?", (row[0],))
            self.db.commit()
        return True

    def _attach(self, model) -> None:
        import numpy as np
        with self.lock:
            if self._meta("embed_model") != model.model_id:      # another model: its vectors do not mix
                self.db.execute("UPDATE chunks SET vec=NULL WHERE vec IS NOT NULL")
                self._set_meta("embed_model", model.model_id)
                self.db.commit()
            vecs = _Vectors(np, model.dim)
            cur = self.db.execute("SELECT cid, vec FROM chunks WHERE vec IS NOT NULL ORDER BY cid")
            while True:
                rows = cur.fetchmany(4096)
                if not rows:
                    break
                rows = [r for r in rows if r[1] and len(r[1]) == model.dim * 2]
                if rows:
                    m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(-1, model.dim)
                    vecs.add([r[0] for r in rows], m.astype(np.float32))
            self._pending = self.db.execute(f"SELECT COUNT(*) FROM chunks WHERE vec IS NULL AND ord < {EMBED_CHUNKS}"
                                            ).fetchone()[0]
        self.vectors, self.embedder = vecs, model
        self.semantic = "indexing" if self._pending else "ready"

    def _embed_batch(self) -> bool:
        with self.lock:
            rows = self.db.execute(
                "SELECT c.cid, c.ord, r.title, substr(d.text, c.start + 1, c.stop - c.start) FROM chunks c "
                "JOIN records r ON r.n = c.rec JOIN docs d ON d.rowid = c.rec "
                f"WHERE c.vec IS NULL AND c.ord < {EMBED_CHUNKS} ORDER BY c.cid LIMIT ?", (EMBED_BATCH,)).fetchall()
        if not rows:
            self._pending = 0
            self.semantic = "ready"
            return False
        texts = [((title + "\n") if title and o == 0 else "") + (text or "") for _, o, title, text in rows]
        t0 = time.time()
        try:
            v = self.embedder.encode(texts)
        except Exception as e:  # noqa: BLE001 - skip what cannot be embedded, keep going
            self.log(f"semantic search: {len(rows)} chunks skipped ({e!r})")
            with self.lock:
                self.db.executemany("UPDATE chunks SET vec=x'' WHERE cid=?", [(r[0],) for r in rows])
                self.db.commit()
            return True
        with self.lock:
            if self._stop.is_set():
                return False
            self.db.executemany("UPDATE chunks SET vec=? WHERE cid=?",
                                [(row.astype("float16").tobytes(), r[0]) for r, row in zip(rows, v)])
            self.db.commit()
            self._pending = self.db.execute(f"SELECT COUNT(*) FROM chunks WHERE vec IS NULL AND ord < {EMBED_CHUNKS}"
                                            ).fetchone()[0]
        self.vectors.add([r[0] for r in rows], v)
        self.semantic = "indexing" if self._pending else "ready"
        self._stop.wait(min(2.0, time.time() - t0))        # at most about half of two cores
        return True

    def dense_ready(self) -> bool:
        if self.embedder is None or self.vectors is None:
            return False
        return self._pending <= max(64, self.vectors.n // 100)

    # ---------------------------------------------------------------- reading
    def _allowed(self, ns, where, args) -> set:
        out = set()
        ns = list(ns)
        for i in range(0, len(ns), 500):
            part = ns[i:i + 500]
            out |= {r[0] for r in self.db.execute(
                f"SELECT n FROM records WHERE n IN ({','.join('?' * len(part))}) AND {where}", part + args)}
        return out

    def _records_from(self, cids, where, args):
        """Chunk ranking -> (record ranking, {record: best chunk}) for the records the scope allows."""
        if not cids:
            return [], {}
        rec_of = {}
        for i in range(0, len(cids), 500):
            part = cids[i:i + 500]
            rec_of.update(self.db.execute(f"SELECT cid, rec FROM chunks WHERE cid IN ({','.join('?' * len(part))})",
                                          part).fetchall())
        allowed = self._allowed({rec_of[c] for c in cids if c in rec_of}, where, args)
        out, best = [], {}
        for c in cids:
            n = rec_of.get(c)
            if n in allowed and n not in best:
                best[n] = c
                out.append(n)
                if len(out) >= LEG_RECORDS:
                    break
        return out, best

    def _lexical(self, query):
        lists = []
        for table, match in (("ctri", trigram_query(query)), ("cbi", bigram_query(query))):
            if not match:
                continue
            try:
                lists.append([r[0] for r in self.db.execute(
                    f"SELECT rowid FROM {table} WHERE {table} MATCH ? ORDER BY bm25({table}, 1.0, 0.5) LIMIT ?",
                    (match, LEG_CHUNKS))])
            except sqlite3.OperationalError:
                pass
        lists = [x for x in lists if x]
        if len(lists) <= 1:
            return lists[0] if lists else []
        fused, score = rrf(lists)
        ts = {}
        for i in range(0, len(fused), 500):
            part = fused[i:i + 500]
            ts.update(self.db.execute(f"SELECT cid, ts FROM chunks WHERE cid IN ({','.join('?' * len(part))})", part))
        fused.sort(key=lambda c: ts.get(c) or "", reverse=True)
        fused.sort(key=lambda c: -round(score[c], 9))
        return fused

    def _chunk_text(self, cid) -> str:
        row = self.db.execute("SELECT substr(d.text, c.start + 1, c.stop - c.start) FROM chunks c "
                              "JOIN docs d ON d.rowid = c.rec WHERE c.cid=?", (cid,)).fetchone()
        return (row[0] or "") if row else ""

    def search(self, query: str, scope: Scope, kinds: list | None = None, limit: int = 24) -> list[dict]:
        if not self.chunks_ready():
            return super().search(query, scope, kinds=kinds, limit=limit)
        where, args = scope.sql()
        if kinds:
            where += f" AND kind IN ({','.join('?' * len(kinds))})"
            args = args + list(kinds)
        qvec, dense_hits = None, []
        if self.dense_ready():
            try:
                qvec = self.embedder.encode([query], query=True)[0]
                dense_hits = self.vectors.top(qvec, LEG_CHUNKS)
            except Exception as e:  # noqa: BLE001 - the word legs still answer
                self.log(f"semantic search skipped: {e!r}")
        cos = dict(dense_hits)
        terms, sentence = query_terms(query)
        with self.lock:
            lex, lex_best = self._records_from(self._lexical(query), where, args)
            den, den_best = self._records_from([c for c, _ in dense_hits], where, args)
            fused, score = rrf([lex, den], weights=(W_LEX, W_DENSE))
            rows, cover = {}, {}
            for n in fused:
                row = self.db.execute("SELECT r.n, r.id, r.ts, r.kind, r.bot, r.project, r.room, r.session, r.shared, "
                                      "r.title, r.ref, r.meta FROM records r WHERE r.n=?", (n,)).fetchone()
                if not row:
                    continue
                search_title = self.db.execute("SELECT title FROM docs WHERE rowid=?", (n,)).fetchone()
                parts = [norm(row[9] or ""), norm(search_title[0] if search_title else "")] + [
                    norm(self._chunk_text(c)) for c in {lex_best.get(n), den_best.get(n)} - {None}]
                text = " ".join(parts)
                hit = sum(1 for t in terms if t in text)
                cover[n] = (hit, hit / len(terms) if terms else 0.0)
                if terms and not sentence and not hit and n not in den_best:
                    continue                                  # only a partial bigram match: not a hit
                rows[n] = row
                score[n] += GAMMA * cover[n][1]
            fused = [n for n in fused if n in rows]
            fused.sort(key=lambda n: rows[n][2] or "", reverse=True)        # newer first on ties
            fused.sort(key=lambda n: -round(score[n], 9))
            out = []
            for n in fused[:limit]:
                text = self.db.execute("SELECT text FROM docs WHERE rowid=?", (n,)).fetchone()
                d = self._row(tuple(rows[n]) + ((text[0] if text else ""),))
                d["matched"], d["coverage"] = cover[n][0], round(cover[n][1], 4)
                d["score"] = float(score[n])
                d["dense"] = round(max(cos[den_best[n]], 0.0), 4) if n in den_best else None
                out.append(d)
        if qvec is not None:                       # cosine for hits the meaning leg did not rank
            missing = [d for d in out if d["dense"] is None]
            if missing:
                with self.lock:
                    cids = {}
                    for d in missing:
                        for (c,) in self.db.execute("SELECT cid FROM chunks WHERE rec=? AND vec IS NOT NULL "
                                                    "AND length(vec) > 0", (d["n"],)):
                            cids[c] = d
                m, ids = self.vectors.rows_for(cids)
                if m is not None and len(ids):
                    for c, s in zip(ids, (m @ qvec).tolist()):
                        d = cids[int(c)]
                        d["dense"] = round(max(s, d["dense"] or 0.0), 4)
        return out
