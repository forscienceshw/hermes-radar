#!/usr/bin/env python
"""Hermes 科研雷达 —— 安装向导。

在一台新电脑上，把这个文件夹解压出来，运行一次这个脚本就够了：

    python install.py

它会做四件事：建虚拟环境、装依赖、问几个配置问题、把数据库建起来。
之后再双击 "Start Radar.vbs" 就能用。

已经装过的话再运行是安全的：不会覆盖你现有的设置，
只在缺东西的时候补。要重新配置加 --reconfigure。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
CONFIG = ROOT / "config"
SETTINGS = CONFIG / "settings.json"
EXAMPLE = CONFIG / "settings.example.json"
REQUIREMENTS = ROOT / "requirements.txt"
MIN_PYTHON = (3, 11)
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

OK = "  [ok]"
FAIL = "  [!!]"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def say(text: str = "") -> None:
    print(text, flush=True)


def step(number: int, title: str) -> None:
    say()
    say(f"── 第 {number} 步 · {title} " + "─" * max(0, 52 - len(title)))


def ask(prompt: str, default: str = "", assume_yes: bool = False) -> str:
    if assume_yes:
        return default
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        say()
        return default
    return answer or default


def confirm(prompt: str, default: bool = False, assume_yes: bool = False) -> bool:
    if assume_yes:
        return default
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"  {prompt} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        say()
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "是", "好", "1")


def ask_secret(prompt: str, assume_yes: bool = False) -> str:
    """密钥不回显，免得留在终端滚动记录里。"""
    if assume_yes:
        return ""
    import getpass
    try:
        return getpass.getpass(f"  {prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        say()
        return ""


def venv_python() -> Path:
    return VENV / "Scripts" / "python.exe" if os.name == "nt" else VENV / "bin" / "python"


def run(args: list[str], timeout: int = 1800, quiet: bool = False) -> int:
    if not quiet:
        say("  $ " + " ".join(args[:4]) + (" …" if len(args) > 4 else ""))
    result = subprocess.run(args, cwd=str(ROOT))
    return result.returncode


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------
# 各步骤
# --------------------------------------------------------------------------

def check_python() -> bool:
    step(1, "运行环境")
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if version[:2] < MIN_PYTHON:
        say(f"{FAIL} 这台电脑的 Python 是 {text}，太旧了。")
        say(f"       请到 https://www.python.org/downloads/ 装 "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更新的版本，")
        say("       安装时记得勾选 \"Add Python to PATH\"。")
        return False
    say(f"{OK} Python {text}（{sys.executable}）")
    return True


def ensure_venv(reinstall: bool) -> bool:
    step(2, "虚拟环境与依赖")
    python = venv_python()
    if python.is_file() and not reinstall:
        say(f"{OK} 虚拟环境已存在：{VENV}")
    else:
        say("  正在创建虚拟环境（大约 30 秒）…")
        base = getattr(sys, "_base_executable", "") or sys.executable
        if python.is_file():
            shutil.rmtree(VENV, ignore_errors=True)
        code = run([base, "-m", "venv", str(VENV)], quiet=True)
        if code != 0 or not python.is_file():
            say(f"{FAIL} 创建虚拟环境失败。")
            say("       常见原因：Python 安装时没装 venv 组件，或目录没有写权限。")
            say(f"       可以手动试：{base} -m venv \"{VENV}\"")
            return False
        say(f"{OK} 虚拟环境已建好：{VENV}")

    if not REQUIREMENTS.is_file():
        say(f"{FAIL} 找不到 requirements.txt，这个文件夹不完整。")
        return False

    say("  检查依赖（第一次会下载几百 MB，慢一点正常）…")
    run([str(python), "-m", "pip", "install", "--upgrade", "pip", "--quiet",
         "--disable-pip-version-check"], quiet=True)
    code = run([str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS),
                "--disable-pip-version-check", "--quiet"], quiet=True)
    if code != 0:
        say("  官方源装不上，换清华镜像再试一次 …")
        code = run([str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS),
                    "-i", MIRROR, "--disable-pip-version-check", "--quiet"], quiet=True)
    if code != 0:
        say(f"{FAIL} 依赖安装失败。")
        say("       检查网络后重试；公司/学校网络可能需要代理。")
        say(f"       手动排查：{python} -m pip install -r requirements.txt")
        return False
    say(f"{OK} 依赖就绪")
    return True


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def set_secret(name: str, value: str) -> bool:
    """交给 app.settings 写，密钥走 Windows DPAPI，只在这台机器上能解。"""
    if not value:
        return True
    python = venv_python()
    code = (
        "import sys;"
        f"sys.path.insert(0,{str(ROOT)!r});"
        "from app.settings import set_secret;"
        f"set_secret({name!r}, {value!r})"
    )
    result = subprocess.run([str(python), "-c", code], cwd=str(ROOT),
                            capture_output=True, timeout=120)
    if result.returncode != 0:
        say(f"  (提示) 密钥没能加密保存：{result.stderr.decode('utf-8', 'replace')[:200]}")
        return False
    return True


def configure(assume_yes: bool, reconfigure: bool) -> bool:
    step(3, "配置")
    if SETTINGS.is_file() and not reconfigure:
        current = read_json(SETTINGS)
        say(f"{OK} 已有配置文件，保留原样：{SETTINGS}")
        say(f"       端口 {current.get('port', 8766)}，"
            f"阅读引擎 {'开' if (current.get('llm') or {}).get('enabled') else '关'}")
        say("       想重新配置：python install.py --reconfigure")
        return True

    settings = read_json(EXAMPLE) if EXAMPLE.is_file() else {"schema_version": 1}
    settings.setdefault("schema_version", 1)

    # ---- 端口 ----
    default_port = 8766
    while True:
        raw = ask("网页端口", str(default_port), assume_yes)
        if not raw.isdigit() or not (1024 <= int(raw) <= 65535):
            say("  端口要是 1024–65535 之间的数字，再试一次。")
            continue
        port = int(raw)
        if not port_free(port):
            say(f"  {port} 被别的程序占着。换一个，或者先关掉占用的程序。")
            if assume_yes:
                port = 8767
                break
            continue
        break
    settings["host"] = "127.0.0.1"
    settings["port"] = port

    # ---- 阅读引擎 ----
    say()
    say("  阅读引擎决定它能不能自动写摘要、做图片分诊。")
    say("    1) WorkBuddy 云端 —— 用本机已登录的 WorkBuddy，不用填 API key（推荐）")
    say("    2) 自己的 API   —— 任何 OpenAI 兼容接口（DeepSeek / 智谱 / OpenAI…）")
    say("    3) Ollama 本地  —— 本机跑着 ollama，免费但慢")
    say("    4) 先不配       —— 只抓取和存档，不写摘要")
    choice = ask("选哪个", "1", assume_yes)

    llm = settings.setdefault("llm", {})
    if choice == "1":
        llm.update({"enabled": True, "provider": "workbuddy_cloud",
                    "model": ask("模型名", "glm-5.3-flash", assume_yes),
                    "vision_enabled": True})
    elif choice == "2":
        llm.update({"enabled": True, "provider": "openai_compatible",
                    "base_url": ask("接口地址", "https://api.deepseek.com/v1", assume_yes),
                    "model": ask("模型名", "deepseek-chat", assume_yes),
                    "vision_enabled": confirm("这个模型能看图吗", False, assume_yes)})
        key = ask_secret("API key（加密后存在本机，不会写进配置文件）", assume_yes)
        if key:
            set_secret("llm_api_key", key)
    elif choice == "3":
        llm.update({"enabled": True, "provider": "ollama",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": ask("模型名", "qwen2.5:14b", assume_yes),
                    "vision_enabled": False})
    else:
        llm["enabled"] = False
    settings["llm"] = llm

    # ---- 论文 ----
    say()
    email = ask("下载论文 PDF 用的联系邮箱（可留空）", "", assume_yes)
    settings.setdefault("papers", {})["email"] = email

    # ---- 推送 ----
    say()
    say("  抓到的文章怎么通知你？")
    say("    1) 只在网页里看（默认，不发消息）")
    say("    2) 企业微信群机器人 webhook")
    say("    3) 不要推送")
    channel = ask("选哪个", "1", assume_yes)
    notifications = settings.setdefault("notifications", {})
    if channel == "2":
        notifications.update({"enabled": True, "channel": "wecom_webhook"})
        hook = ask("webhook 地址", "", assume_yes)
        if hook:
            set_secret("notification_webhook", hook)
    elif channel == "3":
        notifications.update({"enabled": False, "channel": "inbox"})
    else:
        notifications.update({"enabled": False, "channel": "inbox"})
    settings["notifications"] = notifications

    # ---- 微信监测 ----
    say()
    say("  微信公众号自动监测需要：本机装微信 3.x、登录、并且能读它的数据库。")
    say("  这一步比较折腾，可以先用着网页端，以后再开。")
    wechat = confirm("现在就要开吗", False, assume_yes)
    write_json(SETTINGS, settings)
    say(f"{OK} 配置已写入 {SETTINGS}")
    if wechat:
        say("  微信监测还需要单独填数据库密钥：在网页 设置 → 公众号 里配置。")
    return True


def init_database() -> bool:
    step(4, "数据库")
    python = venv_python()
    code = (
        "import sys;"
        f"sys.path.insert(0,{str(ROOT)!r});"
        "from app.paths import DB_PATH;"
        "from app.db import Database;"
        "db=Database(DB_PATH);"
        "print(db.query('SELECT COUNT(*) AS n FROM items')[0]['n'])"
    )
    result = subprocess.run([str(python), "-c", code], cwd=str(ROOT),
                            capture_output=True, timeout=300)
    if result.returncode != 0:
        say(f"{FAIL} 数据库初始化失败：")
        say(result.stderr.decode("utf-8", "replace")[-800:])
        return False
    count = result.stdout.decode("utf-8", "replace").strip() or "0"
    say(f"{OK} 数据库就绪：{ROOT / 'data' / 'radar.db'}（现有 {count} 条）")
    return True


def setup_autostart(assume_yes: bool) -> None:
    step(5, "开机自启")
    if os.name != "nt":
        return
    script = ROOT / "Start Radar.vbs"
    target = f'wscript.exe "{script}"'
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            existing, _ = winreg.QueryValueEx(key, "HermesResearchRadar")
    except OSError:
        existing = ""
    if existing == target:
        say(f"{OK} 已经在自启项里了，不重复添加")
        return
    say("  会在注册表里加一条（只对当前用户，删掉也容易）：")
    say(f"    HKCU\\{key_path}")
    say(f"    HermesResearchRadar = {target}")
    if not confirm("加吗", False, assume_yes):
        say("  跳过。以后想开，可以再运行 python install.py。")
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "HermesResearchRadar", 0, winreg.REG_SZ, target)
    say(f"{OK} 已加。想取消就运行：python radar.py where 看路径，手动删这条注册表值。")


def finish() -> None:
    settings = read_json(SETTINGS)
    port = settings.get("port", 8766)
    step(6, "完成")
    say(f"  打开网页：http://127.0.0.1:{port}")
    say(f"  启动：双击 \"Start Radar.vbs\"")
    say(f"  停止：双击 \"Stop Radar.vbs\"")
    say()
    say("  以后改代码改坏了：")
    say("    python radar.py status          看现在改了什么")
    say("    python radar.py save \"说明\"     存一个版本")
    say("    python radar.py undo            退回上一个版本")
    say()
    say("  这个文件夹可以整个拷到别的电脑，拷过去再跑一次 install.py 就行。")


def main(argv: list[str]) -> int:
    assume_yes = "--yes" in argv or "-y" in argv
    reconfigure = "--reconfigure" in argv
    reinstall = "--reinstall" in argv
    say("=" * 62)
    say("  Hermes 科研雷达 · 安装向导")
    say("=" * 62)
    say("  这个脚本会检查环境、装依赖、问几个配置问题。")
    say("  已经装过的话运行它是安全的，不会覆盖现有设置。")

    if not check_python():
        return 1
    if not ensure_venv(reinstall):
        return 1
    if not configure(assume_yes, reconfigure):
        return 1
    if not init_database():
        return 1
    setup_autostart(assume_yes)
    finish()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\n已取消。")
        raise SystemExit(130)
