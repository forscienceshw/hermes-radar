"""打包：把"能跑起来的那部分"收进一个 zip，换台电脑解压 + 装一次就能用。

刻意用白名单而不是黑名单。data/ 里躺着 3.6G 的离线文档与图片、还有个人数据库，
黑名单漏一个就是几个 G 的误发；白名单漏一个只是少个文件，代价小得多。
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import release_paths
from .vcs import ROOT, Repo, project_version

# 整个目录收进来
INCLUDE_DIRS = (
    "app", "web", "tools", "scripts", "tests", "docs", "installer",
    # 微信只读解密只用到这四个文件；MemoTrace 的 GUI 资源有 38 MB，不要。
    "analysis/wechatmsg_full/app/decrypt",
)
# 单个文件收进来
INCLUDE_FILES = (
    "requirements.txt",
    "VERSION",
    "AGENTS.md",
    "README.md",
    "radar.py",
    "install.py",
    "supervisor.pyw",
    "radar_tray.pyw",
    "start_radar.pyw",
    "stop_radar.pyw",
    "Start Radar.vbs",
    "Stop Radar.vbs",
    "Install.vbs",
    "package.json",
    "package-lock.json",
    # WorkBuddy 点数模式靠这个打包好的 Node 单文件，缺了就只能用 API Key。
    "cloud/dist/llm-worker.mjs",
    "analysis/wechatmsg_full/LICENSE",
)
# cloud/ 只收源码，dist 是构建产物
INCLUDE_GLOBS = ("cloud/*.mjs", "cloud/*.json")
# 任何位置出现都要跳过
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".git", "node_modules", ".venv", "dist", ".tmp", ".cache"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".zip", ".db", ".db-wal", ".db-shm"}


@dataclass
class BuildResult:
    path: Path
    files: int
    bytes: int
    missing: list[str] = field(default_factory=list)
    dirty: bool = False


def _skip(path: Path) -> bool:
    if any(part in SKIP_PARTS for part in path.parts):
        return True
    return path.suffix.lower() in SKIP_SUFFIX


def collect(root: Path = ROOT) -> tuple[list[Path], list[str]]:
    """返回 (要打包的文件, 清单里写了但实际不存在的文件)。"""
    files: list[Path] = []
    missing: list[str] = []
    for name in INCLUDE_DIRS:
        directory = root / name
        if not directory.is_dir():
            missing.append(f"{name}/")
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not _skip(path.relative_to(root)):
                files.append(path)
    for name in INCLUDE_FILES:
        path = root / name
        if path.is_file():
            files.append(path)
        elif name not in ("README.md", "AGENTS.md"):  # 这两个可以先没有
            missing.append(name)
    for pattern in INCLUDE_GLOBS:
        for path in sorted(root.glob(pattern)):
            if path.is_file() and not _skip(path.relative_to(root)):
                files.append(path)
    # 配置模板是给别人的，必须存在
    template = root / "config" / "settings.example.json"
    if template.is_file():
        files.append(template)
    else:
        missing.append("config/settings.example.json")
    # 去重（glob 与目录可能重叠）
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique, missing


def _build_info(root: Path) -> dict:
    repo = Repo(root)
    info = {
        "name": "Hermes 科研雷达",
        "version": project_version(),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": "3.11+（推荐 3.12 / 3.13 / 3.14）",
        "commit": "",
        "dirty": False,
    }
    if repo.has_commits():
        info["commit"] = repo.head_sha()[:8]
        info["dirty"] = bool(repo.pending())
    return info


def build(output: Path | None = None, root: Path = ROOT) -> BuildResult:
    files, missing = collect(root)
    if not files:
        raise RuntimeError("没有收集到任何文件，检查白名单。")
    info = _build_info(root)
    version = info["version"]
    stamp = time.strftime("%Y%m%d")
    if output is None:
        output = release_paths.zip_dir() / f"hermes-radar-{version}-{stamp}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        prefix = f"hermes-radar-{version}"
        for path in files:
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            total += len(data)
            archive.writestr(f"{prefix}/{relative}", data)
        archive.writestr(f"{prefix}/BUILD-INFO.json",
                         json.dumps(info, ensure_ascii=False, indent=2))
        archive.writestr(f"{prefix}/INSTALL.txt", _install_note(version))
    return BuildResult(output, len(files) + 2, total, missing, info["dirty"])


def _install_note(version: str) -> str:
    return f"""Hermes 科研雷达 {version} —— 安装说明
==================================================

一、装依赖（两种方式，选一种）
   A. 双击 "Install.vbs"
      会弹一个黑色窗口，在里面回答几个问题就行。装完窗口会停住，看完再关。
   B. 命令行（如果 A 打不开）
      在命令行里运行：python install.py

   跑之前电脑上要有 Python 3.11 或更新版本。没有的话先去
   https://www.python.org/downloads/ 装一个，安装时勾选 "Add Python to PATH"。

二、启动
   双击 "Start Radar.vbs"，然后浏览器打开 http://127.0.0.1:8766

三、想停
   双击 "Stop Radar.vbs"

四、以后改代码改坏了
   python radar.py status        看当前改动
   python radar.py save "说明"    存一个版本
   python radar.py undo          退回上一个版本
   python radar.py list          看历史版本

五、这个包里没有什么
   - 不含任何文章、图片、数据库（那些在你的 data/ 目录里）
   - 不含密钥。安装向导会问你要，密钥只存在本机，换机器要重填
   - 微信公众号自动监测需要本机装微信 3.x 并解密其数据库，属于可选功能

六、想连资料一起搬
   把原机器的 data/ 整个目录拷到同一个位置，再跑一次 Install.vbs。
   已有数据库不会被覆盖。
"""


def describe(result: BuildResult) -> str:
    lines = [
        f"打包完成：{result.path}",
        f"  版本 {project_version()}    {result.files} 个文件    "
        f"压缩前 {result.bytes / 1024 / 1024:.1f} MB    "
        f"压缩后 {result.path.stat().st_size / 1024 / 1024:.1f} MB",
    ]
    if result.missing:
        lines.append(f"  清单里缺这些（不影响运行，但可能少功能）：{'、'.join(result.missing)}")
    if result.dirty:
        lines.append("  注意：工作区有未保存的改动，这个包里的代码和任何历史版本都对不上。"
                     "建议先 radar.py save。")
    return "\n".join(lines)
