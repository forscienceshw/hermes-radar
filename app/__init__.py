"""Hermes Research Radar: local-first, durable research intelligence."""

from pathlib import Path


def _read_version() -> str:
    """版本号只认仓库根目录的 VERSION 一个来源。

    以前这里是写死的 "0.1.0"，发布页显示 1.0.0、/api/health 却报 0.1.0，
    用户看到两个版本号。让 VERSION 当唯一真相，读不到再退回占位值。
    """
    try:
        text = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8")
        return text.strip().splitlines()[0].strip() or "0.0.0"
    except (OSError, IndexError):
        return "0.0.0"


__version__ = _read_version()
