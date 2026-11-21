"""本地语义向量：bge-small-zh-v1.5 的 int8 量化 ONNX，纯 CPU 推理。

为什么需要它
------------
bigram 只认字面。「ADC」和「抗体偶联药物」一个字都不重合，打分 0，但它们是
同一个东西。检索源本来就爱换说法——同一篇论文，PubMed 写全称、公众号写缩写、
学校通知写中文——纯字面会把对的结果判成不相关，用户看到的就是「搜不到」。

实测（这台机器，int8）：相关标题 0.64–0.71，不相关 0.30，区分度够用；
单条编码 1–3 毫秒，一次检索编码二三十条不到 100 毫秒。

降级是硬要求
------------
别人电脑上可能没有模型、没装 onnxruntime、或者下载失败。这三种情况一律返回
None，调用方退回字面打分，检索照常工作。**不能因为少一个优化项就让功能不可用。**

模型来源：hf-mirror.com/Xenova/bge-small-zh-v1.5（ONNX 转换版），
文件放 data/models/bge-small-zh-v1.5/{model_int8.onnx,vocab.txt}。
首次缺失时可在设置页点「补下语义模型」，或跑 `python -m app.services.embedding --fetch`。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterable

from ..paths import DATA_DIR

MODEL_DIR = DATA_DIR / "models" / "bge-small-zh-v1.5"
MODEL_FILE = MODEL_DIR / "model_int8.onnx"
VOCAB_FILE = MODEL_DIR / "vocab.txt"
MAX_LEN = 256
MAX_BATCH = 64

# 下载地址按可达性排序：hf-mirror 国内直连，HuggingFace 原站留作墙外兜底。
SOURCES = (
    ("https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/onnx/model_int8.onnx", MODEL_FILE, 24_000_000),
    ("https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/vocab.txt", VOCAB_FILE, 110_000),
    ("https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main/onnx/model_int8.onnx", MODEL_FILE, 24_000_000),
    ("https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main/vocab.txt", VOCAB_FILE, 110_000),
)

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"session": None, "vocab": None, "tried": False, "reason": ""}


def is_cjk(char: str) -> bool:
    code = ord(char)
    return (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
            or 0xF900 <= code <= 0xFAFF or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF)


def _wordpiece(word: str, vocab: dict[str, int], limit: int = 12) -> list[int]:
    """BERT 的贪心最长匹配。中文词表里汉字基本都是单字，所以主要吃英文。"""
    if not word:
        return []
    if word in vocab:
        return [vocab[word]]
    out: list[int] = []
    start = 0
    unknown = vocab.get("[UNK]", 100)
    while start < len(word) and len(out) < limit:
        end = len(word)
        piece = None
        while start < end:
            candidate = word[start:end]
            if start > 0:
                candidate = "##" + candidate
            if candidate in vocab:
                piece = vocab[candidate]
                break
            end -= 1
        if piece is None:
            return [unknown]
        out.append(piece)
        start = end
    return out


def tokenize(text: str, vocab: dict[str, int]) -> list[int]:
    """按 BERT 中文规则切：汉字逐字，英文与数字走 WordPiece。"""
    ids = [vocab.get("[CLS]", 101)]
    buffer = ""
    for char in text or "":
        if is_cjk(char):
            if buffer:
                ids.extend(_wordpiece(buffer.lower(), vocab))
                buffer = ""
            ids.extend(_wordpiece(char, vocab))
        elif char.isalnum():
            buffer += char
        else:
            if buffer:
                ids.extend(_wordpiece(buffer.lower(), vocab))
                buffer = ""
            stripped = char.strip()
            if stripped:
                ids.extend(_wordpiece(stripped, vocab))
    if buffer:
        ids.extend(_wordpiece(buffer.lower(), vocab))
    ids = ids[: MAX_LEN - 1] + [vocab.get("[SEP]", 102)]
    return ids


def _load() -> bool:
    """加载模型与词表，只尝试一次。失败原因留在 _STATE 里给诊断用。"""
    if _STATE["tried"]:
        return _STATE["session"] is not None
    with _LOCK:
        if _STATE["tried"]:
            return _STATE["session"] is not None
        _STATE["tried"] = True
        if not MODEL_FILE.is_file() or not VOCAB_FILE.is_file():
            _STATE["reason"] = "模型文件不存在"
            return False
        try:
            import onnxruntime  # noqa: F401 - 只是探依赖
        except Exception as exc:                     # noqa: BLE001
            _STATE["reason"] = f"没装 onnxruntime（{type(exc).__name__}）"
            return False
        try:
            import onnxruntime as ort
            vocab: dict[str, int] = {}
            with VOCAB_FILE.open("r", encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle):
                    vocab[line.rstrip("\n")] = index
            session = ort.InferenceSession(str(MODEL_FILE), providers=["CPUExecutionProvider"])
            if not vocab or session is None:
                raise RuntimeError("词表为空")
        except Exception as exc:                     # noqa: BLE001
            _STATE["reason"] = f"加载失败：{type(exc).__name__}: {exc}"
            return False
        _STATE.update({"session": session, "vocab": vocab, "reason": ""})
        return True


def available() -> bool:
    return _load()


def reason() -> str:
    """没启用的原因，给设置页和 /api/net_status 这类诊断口用。"""
    _load()
    return str(_STATE["reason"] or "")


def model_size() -> int:
    try:
        return MODEL_FILE.stat().st_size
    except OSError:
        return 0


def encode(texts: Iterable[str]) -> list[list[float]] | None:
    """把一批文本编码成单位向量。不可用时返回 None，调用方必须能接住。"""
    if not _load():
        return None
    rows = [str(text or "")[:1200] for text in texts]
    if not rows:
        return []
    import numpy as np

    session = _STATE["session"]
    vocab = _STATE["vocab"]
    out: list[list[float]] = []
    for start in range(0, len(rows), MAX_BATCH):
        chunk = rows[start:start + MAX_BATCH]
        tokenized = [tokenize(text, vocab) for text in chunk]
        width = max(len(seq) for seq in tokenized) or 1
        ids = np.zeros((len(chunk), width), dtype=np.int64)
        mask = np.zeros((len(chunk), width), dtype=np.int64)
        for row, seq in enumerate(tokenized):
            ids[row, :len(seq)] = seq
            mask[row, :len(seq)] = 1
        feeds = {"input_ids": ids, "attention_mask": mask,
                 "token_type_ids": np.zeros_like(ids)}
        try:
            hidden = session.run(None, feeds)[0][:, 0, :]      # [CLS] 池化
        except Exception:                                    # noqa: BLE001 - 推理挂了就当没有
            _STATE.update({"session": None, "reason": "推理失败，已停用"})
            return None
        norm = np.linalg.norm(hidden, axis=1, keepdims=True)
        hidden = hidden / np.maximum(norm, 1e-9)
        out.extend(hidden.astype("float32").tolist())
    return out


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


def fetch(force: bool = False) -> dict[str, Any]:
    """下载模型与词表。给设置页的按钮用，返回人话结果。"""
    import httpx

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    done: list[str] = []
    for url, target, expected in SOURCES:
        if target.is_file() and target.stat().st_size > expected * 0.8 and not force:
            done.append(f"{target.name} 已存在")
            continue
        try:
            with httpx.stream("GET", url, timeout=httpx.Timeout(connect=15.0, read=120.0,
                                                                 write=30.0, pool=15.0),
                              follow_redirects=True) as response:
                if response.status_code != 200:
                    continue
                temp = target.with_suffix(target.suffix + ".part")
                with temp.open("wb") as handle:
                    for block in response.iter_bytes(1 << 16):
                        handle.write(block)
                if temp.stat().st_size < expected * 0.5:
                    temp.unlink(missing_ok=True)
                    continue
                temp.replace(target)
            done.append(f"{target.name} 下载完成")
        except Exception:                            # noqa: BLE001 - 换下一个源
            continue
        if MODEL_FILE.is_file() and VOCAB_FILE.is_file():
            break
    ok = MODEL_FILE.is_file() and VOCAB_FILE.is_file()
    _STATE.update({"tried": False, "session": None})
    return {"ok": ok, "detail": "；".join(done) or "所有下载源都不可达",
            "size": model_size(), "available": available()}


if __name__ == "__main__":                       # pragma: no cover - 手动补模型用
    import json
    import sys
    print(json.dumps(fetch(force="--force" in sys.argv), ensure_ascii=False, indent=2))
