"""把源码打成两个 exe：Radar.exe（主程序入口）和 RadarSetup.exe（安装向导）。

    python tools/build_exe.py            # 两个都打
    python tools/build_exe.py --launcher # 只打主程序
    python tools/build_exe.py --setup    # 只打安装向导

依赖 PyInstaller。为了不污染运行时的 .venv，建议单独建一个构建环境：

    python -m venv E:\\HermesRadarBuild\\.venv
    E:\\HermesRadarBuild\\.venv\\Scripts\\python -m pip install pyinstaller
    E:\\HermesRadarBuild\\.venv\\Scripts\\python tools\\build_exe.py

产物在 dist\\exe\\ 下。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "dist" / "exe"
BUILD = ROOT / ".tmp" / "pyinstaller"
ICON = ROOT / "assets" / "radar.ico"


def ensure_icon() -> Path | None:
    """没有图标就现画一个：深青色圆角方块 + 三道雷达弧 + 一个亮点。"""
    if ICON.is_file():
        return ICON
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    ICON.parent.mkdir(parents=True, exist_ok=True)
    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, size - 12, size - 12), radius=52, fill=(18, 110, 103, 255))
    centre = (128, 128)
    for radius in (46, 76, 106):
        draw.arc((centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius),
                 start=200, end=340, fill=(255, 255, 255, 235), width=10)
    draw.ellipse((118, 118, 138, 138), fill=(255, 255, 255, 255))
    draw.line((128, 128, 196, 74), fill=(255, 255, 255, 210), width=6)
    image.save(ICON, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ICON


def pyinstaller(*args: str) -> None:
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", *args]
    print(">", " ".join(command))
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller 失败（{result.returncode}）")


def build_launcher() -> Path:
    icon = ensure_icon()
    args = [
        "--onefile", "--windowed", "--noupx",
        "--name", "Radar",
        "--distpath", str(OUT),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        str(ROOT / "installer" / "launcher.py"),
    ]
    if icon:
        args[-1:-1] = ["--icon", str(icon)]
    pyinstaller(*args)
    return OUT / "Radar.exe"


def build_payload() -> Path:
    """复用打包模块的白名单，保证 exe 和 zip 两种分发内容一致。

    和 zip 发行版的区别只有一个：这里不套 hermes-radar-x.y.z/ 这层目录，
    因为安装向导是直接往用户选的目录里解压的。
    """
    from tools import packaging

    files, missing = packaging.collect(ROOT)
    if not files:
        raise SystemExit("打包白名单没有收集到任何文件")
    target = OUT / "payload.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            data = path.read_bytes()
            total += len(data)
            archive.writestr(path.relative_to(ROOT).as_posix(), data)
    print(f"payload.zip：{len(files)} 个文件，{total / 1024 / 1024:.1f} MB 原始体积")
    if missing:
        print("缺少（不影响构建）：" + "、".join(missing))
    return target


def build_setup(launcher: Path, payload: Path) -> Path:
    icon = ensure_icon()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0]
    args = [
        "--onefile", "--windowed", "--noupx",
        "--name", "RadarSetup",
        "--distpath", str(OUT),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--add-data", f"{payload};.",
        "--add-data", f"{launcher};.",
        "--add-data", f"{ROOT / 'VERSION'};.",
    ]
    if icon:
        args[-1:-1] = ["--icon", str(icon)]
    args.append(str(ROOT / "installer" / "setup_gui.py"))
    pyinstaller(*args)
    print(f"安装包版本 {version}")
    return OUT / "RadarSetup.exe"


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Radar.exe 与 RadarSetup.exe")
    parser.add_argument("--launcher", action="store_true", help="只构建 Radar.exe")
    parser.add_argument("--setup", action="store_true", help="只构建 RadarSetup.exe")
    args = parser.parse_args()
    only_launcher = args.launcher and not args.setup
    only_setup = args.setup and not args.launcher

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller。先建一个构建环境：\n"
              "  python -m venv E:\\HermesRadarBuild\\.venv\n"
              "  E:\\HermesRadarBuild\\.venv\\Scripts\\python -m pip install pyinstaller\n"
              "  E:\\HermesRadarBuild\\.venv\\Scripts\\python tools\\build_exe.py")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    launcher = OUT / "Radar.exe"
    if not only_setup:
        launcher = build_launcher()
        print(f"Radar.exe：{launcher.stat().st_size / 1024 / 1024:.1f} MB")
    if not only_launcher:
        if not launcher.is_file():
            raise SystemExit("先构建 Radar.exe，再打安装包")
        setup = build_setup(launcher, build_payload())
        print(f"RadarSetup.exe：{setup.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"\n产物目录：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
