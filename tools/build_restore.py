"""把微信回退工具打成 exe。

    python tools/build_restore.py

产物：dist/exe/恢复微信3.9.9.43.exe（约 10 MB）

备份包不塞进 exe：178 MB 的数据如果走 PyInstaller 的 onefile，每次启动都要先
解到临时目录，慢且费磁盘。改成运行时找同目录的 wechat-backup.zip，exe 保持
轻量，用户拿走一个文件夹就行。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist" / "exe"
BUILD = ROOT / ".tmp" / "pyinstaller"
TARGET = OUT / "恢复微信3.9.9.43.exe"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller。用构建环境跑：\n"
              "  E:\\HermesRadarBuild\\.venv\\Scripts\\python tools\\build_restore.py")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--noupx",
        "--name", "恢复微信3.9.9.43",
        "--distpath", str(OUT),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        str(ROOT / "installer" / "restore_wechat.py"),
    ]
    print(">", " ".join(command))
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        print("PyInstaller 失败")
        return 1
    if TARGET.is_file():
        print(f"\n产物：{TARGET}  {TARGET.stat().st_size / 1024 / 1024:.1f} MB")
    else:
        print("没找到产物，检查 --name 里的中文是否被 shell 改写")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
