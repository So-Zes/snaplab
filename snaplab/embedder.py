"""Local text embeddings for recall's semantic leg: multilingual-e5-small (int8 ONNX) on the CPU.

The model files come from Hugging Face, pinned to one commit and checked against their SHA-256, into
data/models/multilingual-e5-small/. Everything here is optional: without numpy, onnxruntime and tokenizers, or without
the files, recall keeps working with its word search alone. "recall": {"semantic": false} turns this off.
The 2026-11-21 benchmark on a real archive chose this model and these settings (see docs/MAP.md).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import urllib.request
from pathlib import Path

REPO = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
FILES = {
    "onnx/model_qint8_avx512_vnni.onnx": (118346824, "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"),
    "tokenizer.json": (17082730, "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
}
MODEL_ID = f"{REPO}@{REVISION[:12]}"
DIM = 384
MAX_TOKENS = 512
PACKAGES = ("numpy", "onnxruntime", "tokenizers")
NO_DOWNLOAD_ENV = "SNAPLAB_NO_MODEL_DOWNLOAD"       # set by the tests: never fetch the model


def missing_packages() -> list[str]:
    return [p for p in PACKAGES if importlib.util.find_spec(p) is None]


def model_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "models" / "multilingual-e5-small"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _marker(folder: Path) -> dict:
    try:
        return json.loads((folder / ".verified.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def files_ready(folder: Path) -> bool:
    """True when every file is present with the size and the hash recorded when it was checked."""
    mark = _marker(folder)
    for name, (size, sha) in FILES.items():
        p = folder / name
        try:
            st = p.stat()
        except OSError:
            return False
        if st.st_size != size or mark.get(name) != [size, st.st_mtime_ns, sha]:
            return False
    return True


def ensure_model(folder: Path, log=None, download: bool = True, opener=None, stop=None) -> bool:
    """Make sure the pinned model files are in folder. Downloads what is missing (unless download is False or the
    tests' environment variable is set), checks size and SHA-256, and moves each file into place in one step.
    Returns True when the model is ready; never raises."""
    log = log or (lambda line: None)
    folder = Path(folder)
    if files_ready(folder):
        return True
    if os.environ.get(NO_DOWNLOAD_ENV):
        download = False
    opener = opener or (lambda url: urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "SnapLab"}), timeout=60))
    mark = _marker(folder)
    try:
        for name, (size, sha) in FILES.items():
            dest = folder / name
            if dest.exists() and dest.stat().st_size == size and _sha256(dest) == sha:
                mark[name] = [size, dest.stat().st_mtime_ns, sha]
                continue
            if not download:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + f".part-{os.getpid()}")
            h, got = hashlib.sha256(), 0
            url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}"
            with opener(url) as resp, open(part, "wb") as out:
                while True:
                    if stop is not None and stop.is_set():
                        raise InterruptedError("stopped")
                    block = resp.read(1 << 20)
                    if not block:
                        break
                    h.update(block)
                    got += len(block)
                    out.write(block)
            if got != size or h.hexdigest() != sha:
                part.unlink(missing_ok=True)
                log(f"semantic search off: {name} from Hugging Face did not match its pinned SHA-256")
                return False
            os.replace(part, dest)
            mark[name] = [size, dest.stat().st_mtime_ns, sha]
        tmp = folder / ".verified.json.tmp"
        tmp.write_text(json.dumps(mark), encoding="utf-8")
        os.replace(tmp, folder / ".verified.json")
        return True
    except InterruptedError:
        return False
    except Exception as e:  # noqa: BLE001 - a network or disk problem only turns the semantic leg off
        log(f"semantic search off: the model could not be fetched ({e!r})")
        for p in folder.rglob("*.part-*"):
            try:
                p.unlink()
            except OSError:
                pass
        return False


class E5:
    """multilingual-e5-small: "query: " / "passage: " prefixes, mean pooling, L2-normalised 384-d float32 vectors."""
    dim = DIM
    model_id = MODEL_ID

    def __init__(self, folder: Path, threads: int = 2):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        folder = Path(folder)
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(threads))
        so.inter_op_num_threads = 1
        so.enable_cpu_mem_arena = False                       # keeps RAM flat between batches
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")   # idle threads do not burn CPU
        self.np = np
        self.sess = ort.InferenceSession(str(folder / "onnx" / "model_qint8_avx512_vnni.onnx"), so,
                                         providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.tok = Tokenizer.from_file(str(folder / "tokenizer.json"))
        self.tok.enable_truncation(MAX_TOKENS)
        pad = self.tok.token_to_id("<pad>")
        self.tok.enable_padding(pad_id=1 if pad is None else pad, pad_token="<pad>")

    def encode(self, texts: list[str], query: bool = False, batch: int = 16):
        np = self.np
        texts = [("query: " if query else "passage: ") + t for t in texts]
        out = []
        for i in range(0, len(texts), batch):
            enc = self.tok.encode_batch(texts[i:i + batch])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            h = self.sess.run(None, feed)[0]
            m = mask[..., None].astype(np.float32)
            v = (h * m).sum(1) / np.maximum(m.sum(1), 1e-9)
            out.append(v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12))
        return np.concatenate(out).astype(np.float32) if out else np.zeros((0, DIM), np.float32)
