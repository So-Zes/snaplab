"""Round 21: the default recall index (chunks, bigram + trigram word search, optional meaning search) and the model
download. Offline: a tiny fake embedder stands in for multilingual-e5-small."""
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import make_app, onboard, wait_until

from snaplab import embedder
from snaplab import hybrid_index as hx
from snaplab.archive import FtsIndex, Scope
from snaplab.temporal import TemporalScope


def rec(i, text, kind="message", ts=None, **kw):
    return {"id": f"a-{i:08x}", "ts": ts or f"2026-11-{1 + i % 20:02d}T10:00:00+09:00", "kind": kind, "text": text,
            "title": kw.pop("title", ""), "meta": {}, **kw}


class FakeEmbedder:
    """Vectors from topic words: 'tide', 'mud', 'shore' and the Japanese 潮 / 干潟 share one direction."""
    model_id = "fake@1"
    dim = 384
    TOPICS = {0: ("tide", "mud", "shore", "潮", "干潟"), 1: ("invoice", "payment", "請求"), 2: ("pytest", "test")}

    def __init__(self):
        import numpy as np
        self.np = np
        self.calls = 0

    def encode(self, texts, query=False, batch=16):
        np = self.np
        self.calls += 1
        out = np.zeros((len(texts), self.dim), np.float32)
        for i, t in enumerate(texts):
            low = t.lower()
            for k, words in self.TOPICS.items():
                out[i, k] = sum(low.count(w) for w in words)
            out[i, 10 + len(low) % 50] += 0.05                         # a little noise so nothing is all zero
            out[i] /= max(np.linalg.norm(out[i]), 1e-9)
        return out


def tscope(scope=None):
    return TemporalScope(scope or Scope(everything=True), "all", None, None)


try:
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


class HybridIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="snaplab-hx-")
        self.path = Path(self.tmp) / "index.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chunks_short_japanese_words_and_hit_fields(self):
        idx = hx.HybridIndex(self.path)
        long = "前置き。" + "あいうえお" * 400 + "泥地のメッシュを直した。" + "かきくけこ" * 400
        idx.add(rec(1, long, kind="doc", title="KNOWLEDGE.md"))
        idx.add(rec(2, "The calculator tests use pytest."))
        idx.add(rec(3, "空の帯割れを直す"))
        n = idx.db.execute("SELECT COUNT(*) FROM chunks WHERE rec=(SELECT n FROM records WHERE id='a-00000001')")
        self.assertGreater(n.fetchone()[0], 3)                         # a long record is several chunks
        self.assertEqual([h["id"] for h in idx.search("泥", tscope())], ["a-00000001"])   # one character
        self.assertEqual([h["id"] for h in idx.search("空", tscope())], ["a-00000003"])
        hit = idx.search("pytest calculator", tscope())[0]
        self.assertEqual(hit["id"], "a-00000002")
        self.assertEqual((hit["matched"], hit["coverage"], hit["dense"]), (2, 1.0, None))
        self.assertIn("score", hit)
        self.assertIn("pytest", hit["text"])                           # the whole record, as FtsIndex gives it
        self.assertEqual(idx.search("zzzz-nothing", tscope()), [])
        idx.close()

    def test_scope_and_kinds_still_apply(self):
        idx = hx.HybridIndex(self.path)
        idx.add(rec(1, "zebra password idea", bot="b-lead"))
        idx.add(rec(2, "zebra stripes theme", bot="b-ken"))
        idx.add(rec(3, "zebra page", kind="page", bot="b-ken"))
        ken = tscope(Scope(bot="b-ken"))
        self.assertEqual(sorted(h["id"] for h in idx.search("zebra", ken)), ["a-00000002", "a-00000003"])
        self.assertEqual([h["id"] for h in idx.search("zebra", ken, kinds=["page"])], ["a-00000003"])
        idx.close()

    def test_an_old_index_is_upgraded_in_the_background_and_resumes(self):
        old = FtsIndex(self.path)
        for i in range(1, 41):
            old.add(rec(i, f"record number {i} about the harbour" + (" lighthouse" if i == 7 else "")))
        old.db.execute("PRAGMA user_version=3")
        old.db.commit()
        before = [h["id"] for h in old.search("lighthouse harbour", tscope())]
        old.close()
        with patch.object(hx, "BACKFILL_BATCH", 10):
            idx = hx.HybridIndex(self.path)
            self.assertFalse(idx.chunks_ready())
            self.assertEqual([h["id"] for h in idx.search("lighthouse harbour", tscope())], before)  # FtsIndex meanwhile
            idx.add(rec(99, "a new lighthouse note"))                 # new records are written as usual
            real_set = idx._set_meta

            def one_batch_then_stop(k, v):                             # a restart after the first batch
                real_set(k, v)
                if k == "chunk_done":
                    idx._stop.set()
            idx._set_meta = one_batch_then_stop
            idx._backfill()
            idx.close()
            idx = hx.HybridIndex(self.path)
            self.assertEqual(idx._done, 10)                            # progress survived the restart
            idx.start()
            self.assertTrue(wait_until(idx.chunks_ready, 10))
        hits = [h["id"] for h in idx.search("lighthouse", tscope())]
        self.assertEqual(sorted(hits), ["a-00000007", "a-00000063"])
        self.assertEqual(idx.db.execute("SELECT COUNT(DISTINCT rec) FROM chunks").fetchone()[0], 41)
        idx.close()

    @unittest.skipUnless(HAVE_NUMPY, "numpy is not installed")
    def test_meaning_leg_finds_other_words_and_reports_cosine(self):
        idx = hx.HybridIndex(self.path)
        idx.add(rec(1, "干潟の泥の表現を直した"))
        idx.add(rec(2, "The invoice was paid by card."))
        idx.add(rec(3, "We use pytest for the calculator."))
        fake = FakeEmbedder()
        idx.start(lambda: fake)
        self.assertTrue(wait_until(lambda: idx.semantic == "ready", 10))
        self.assertEqual(idx.status()["semantic"], "ready")
        hits = idx.search("tide shore mud", tscope())
        self.assertEqual(hits[0]["id"], "a-00000001")                  # no shared word: found by meaning
        self.assertGreater(hits[0]["dense"], 0.9)
        self.assertEqual(hits[0]["coverage"], 0.0)
        top = idx.search("pytest", tscope())[0]
        self.assertEqual((top["id"], top["coverage"]), ("a-00000003", 1.0))
        self.assertIsNotNone(top["dense"])
        idx.add(rec(4, "請求書の支払い"))                                  # new records get vectors in the background
        self.assertTrue(wait_until(lambda: idx.vectors.n == 4, 10))
        idx.close()
        idx = hx.HybridIndex(self.path)                                  # vectors are kept in the file
        idx.start(lambda: FakeEmbedder())
        self.assertTrue(wait_until(lambda: idx.semantic == "ready", 10))
        self.assertEqual(idx.vectors.n, 4)
        idx.close()

    @unittest.skipUnless(HAVE_NUMPY, "numpy is not installed")
    def test_vectors_wait_while_a_chat_turn_runs(self):
        busy = {"on": True}
        idx = hx.HybridIndex(self.path, busy=lambda: busy["on"])
        idx.add(rec(1, "tide and mud"))
        fake = FakeEmbedder()
        idx.start(lambda: fake)
        time.sleep(0.5)
        self.assertEqual(idx.vectors.n if idx.vectors else 0, 0)
        busy["on"] = False
        self.assertTrue(wait_until(lambda: idx.vectors and idx.vectors.n == 1, 10))
        idx.close()


class EmbedderDownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="snaplab-emb-"))
        self.blobs = {"onnx/model_qint8_avx512_vnni.onnx": b"model-bytes" * 100, "tokenizer.json": b'{"tok": 1}'}
        self.files = {n: (len(b), hashlib.sha256(b).hexdigest()) for n, b in self.blobs.items()}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def opener(self, corrupt=None):
        seen = []

        def op(url):
            seen.append(url)
            name = url.split(f"/resolve/{embedder.REVISION}/", 1)[1]
            data = self.blobs[name] if name != corrupt else b"x" + self.blobs[name][1:]
            return io.BytesIO(data)
        return op, seen

    def test_pinned_files_are_checked_and_moved_into_place(self):
        op, seen = self.opener()
        with patch.object(embedder, "FILES", self.files), patch.dict(os.environ, {embedder.NO_DOWNLOAD_ENV: ""}):
            self.assertTrue(embedder.ensure_model(self.tmp, opener=op))
            self.assertTrue(all(embedder.REVISION in u for u in seen))
            self.assertTrue(embedder.files_ready(self.tmp))
            self.assertEqual(sorted(json.loads((self.tmp / ".verified.json").read_text())), sorted(self.blobs))
            seen.clear()
            self.assertTrue(embedder.ensure_model(self.tmp, opener=op))
            self.assertEqual(seen, [])                                   # already there: nothing fetched

    def test_a_wrong_hash_or_no_network_leaves_nothing_behind(self):
        lines = []
        op, _ = self.opener(corrupt="tokenizer.json")
        with patch.object(embedder, "FILES", self.files), patch.dict(os.environ, {embedder.NO_DOWNLOAD_ENV: ""}):
            self.assertFalse(embedder.ensure_model(self.tmp, log=lines.append, opener=op))
            self.assertFalse((self.tmp / "tokenizer.json").exists())
            self.assertFalse(list(self.tmp.rglob("*.part-*")))
            self.assertIn("SHA-256", lines[-1])

            def broken(url):
                raise OSError("no network")
            self.assertFalse(embedder.ensure_model(self.tmp, log=lines.append, opener=broken))
            self.assertIn("could not be fetched", lines[-1])
        with patch.object(embedder, "FILES", self.files):            # the tests' switch: no download at all
            self.assertFalse(embedder.ensure_model(self.tmp, opener=self.opener()[0]))


class ArchiveDefaultIndexTest(unittest.TestCase):
    def test_default_index_is_hybrid_and_semantic_can_be_turned_off(self):
        app, _, _ = make_app()
        onboard(app)
        self.assertEqual(type(app.archive.index).__name__, "HybridIndex")
        self.assertTrue(wait_until(lambda: app.archive.index.semantic in ("unavailable", "off"), 10))
        self.assertIn("index", app.archive.stats())
        app2, _, _ = make_app(recall={"semantic": False})
        self.assertEqual(app2.archive.index.semantic, "off")
        onboard(app2)
        room = app2.store.dm_room(app2.store.orchestrator()["id"])["id"]
        app2.store.add_message(room, {"role": "user", "text": "the lighthouse keeper's schedule"})
        hits = app2.archive.search("lighthouse", Scope(everything=True))
        self.assertTrue(hits and hits[0]["kind"] == "message")

    def test_a_plug_in_index_still_wins(self):
        app, _, _ = make_app(recall={"index": "snaplab.archive:FtsIndex"})
        self.assertEqual(type(app.archive.index).__name__, "FtsIndex")


if __name__ == "__main__":
    unittest.main()
