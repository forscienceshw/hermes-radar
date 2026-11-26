"""发布包放哪儿 —— 与源码仓库、软件安装目录彻底分开。

用户要求安装包不要和「软件安装目录」混在一起。这里定成一条独立路径：

    <项目所在盘的根>\\RadarReleases\\v1.1.0\\RadarSetup.exe

为什么放盘的根而不是某个固定绝对路径：开发目录 `E:\\HermesRadarDev` 与
生产目录 `E:\\HermesResearchRadar` 在同一块盘上，`Path.parent` 都是 `E:\\`，
两边算出来是同一个发布目录，不会各发各的。换台机器（D 盘）也自动跟着走。

要改位置就设环境变量 RADAR_RELEASE_DIR，不用改代码。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def release_dir() -> Path:
    override = os.environ.get("RADAR_RELEASE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (ROOT.parent / "RadarReleases").resolve()


def version_dir(version: str) -> Path:
    return release_dir() / f"v{version.strip()}"


def build_dir() -> Path:
    """PyInstaller 的中间目录。构建成功后会整个删掉，不留垃圾。"""
    return release_dir() / ".build"


def zip_dir() -> Path:
    """`python radar.py pack` 的源码压缩包也放这里，别塞进仓库。"""
    return release_dir() / "zip"


def describe() -> str:
    return str(release_dir())
