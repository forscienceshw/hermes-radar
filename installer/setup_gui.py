"""RadarSetup.exe —— 图形安装向导（也支持静默安装）。

它把「解压 + 建虚拟环境 + 装依赖 + 建快捷方式 + 登记卸载」这一串手工步骤
换成点几下鼠标。安装目录、端口、是否开机自启都在第一页选。

    双击运行              图形向导
    RadarSetup.exe /S --dir=D:\\Radar --port=8899 --no-autostart
                          静默安装，给批量部署用（日志打印到控制台）

打包时 payload.zip、Radar.exe、VERSION 会作为数据塞进这个 exe；运行时它们
出现在 sys._MEIPASS 里。装完之后，用户真正双击的是安装目录里的 Radar.exe。
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Hermes 科研雷达"
APP_ID = "HermesResearchRadar"
PUBLISHER = "Hermes"
MIN_PYTHON = (3, 10)
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
# 装过哪些文件、依赖有没有变。升级时靠它精确删掉"新版不再需要"的旧文件，
# 而不是粗暴地把目录清空 —— 用户的 data/ 和 config/ 就在同一个目录里。
RECORD_NAME = "install.json"
MANIFEST_NAME = ".payload-manifest.json"
KEEP_ON_UPGRADE = {"data", "config", ".venv", "node_modules", "logs", "inbox", "analysis"}


class SetupError(RuntimeError):
    pass


def bundle_dir() -> Path:
    # RADAR_PAYLOAD_DIR 只给开发和自动化测试用：直接指向 dist/exe，省去反复打包。
    override = os.environ.get("RADAR_PAYLOAD_DIR", "").strip()
    if override:
        return Path(override)
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def default_target() -> Path:
    # 装过一次之后就以"上次装在哪儿"为准：同一个目录顶替，绝不另起一份。
    recorded = recorded_target()
    if recorded:
        return recorded
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_ID


def recorded_target() -> Path | None:
    """先看注册表里的 InstallLocation，再看默认目录里的 install.json。"""
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
                location, _ = winreg.QueryValueEx(key, "InstallLocation")
            path = Path(str(location))
            if path.is_dir():
                return path
        except OSError:
            pass
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    fallback = Path(base) / APP_ID
    if (fallback / RECORD_NAME).is_file():
        return fallback
    return None


def read_record(target: Path) -> dict:
    try:
        data = json.loads((target / RECORD_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# 升级：停旧实例、精确清理旧文件
# --------------------------------------------------------------------------- #
def _instance_processes(target: Path) -> list:
    """找出这个安装目录里的进程。

    只认"程序本体"：可执行文件落在安装目录下（Radar.exe、.venv 里的 python），
    或者进程名是 Radar/python 且工作目录就在安装目录里（supervisor 是这么起的）。

    故意不匹配命令行 —— 早先版本用 cmdline 包含路径来判断，结果把正在执行安装
    命令的那个 shell 也算了进去，安装器随后把父进程杀掉，自己跟着被 SIGTERM。
    同一台机器上还可能有开发实例在跑，也不能按进程名一刀切。
    """
    try:
        import psutil
    except ImportError:
        return []
    marker = str(target).casefold().replace("\\", "/").rstrip("/")
    allowed = {"radar.exe", "python.exe", "pythonw.exe"}
    found = []
    for proc in psutil.process_iter(["pid", "name", "cwd"]):
        if proc.pid == os.getpid():
            continue
        if (proc.info.get("name") or "").lower() not in allowed:
            continue
        try:
            executable = str(proc.exe() or "").casefold().replace("\\", "/")
        except (psutil.Error, OSError):
            executable = ""
        if executable.startswith(marker):
            found.append(proc)
            continue
        try:
            cwd = str(proc.info.get("cwd") or "").casefold().replace("\\", "/")
        except (psutil.Error, OSError):
            cwd = ""
        if cwd == marker or cwd.startswith(marker + "/"):
            found.append(proc)
    return found


def stop_running(target: Path, log: Callable[[str], None], timeout: float = 18.0) -> None:
    """先礼后兵：写停止信号让守护进程自己收工，等不到再强杀。"""
    processes = _instance_processes(target)
    if not processes:
        return
    log(f"检测到 {len(processes)} 个正在运行的进程，先让它们退出去…")
    stop_file = target / "data" / "STOP_RADAR"
    try:
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("upgrade", encoding="utf-8")
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _instance_processes(target):
            break
        time.sleep(0.5)
    remaining = _instance_processes(target)
    for proc in remaining:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    if remaining:
        try:
            import psutil
            psutil.wait_procs(remaining, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        log(f"已强制结束 {len(remaining)} 个进程")
    try:
        stop_file.unlink()
    except OSError:
        pass


def payload_members() -> list[str]:
    """payload.zip 里的相对路径清单（统一用 / 分隔）。"""
    with zipfile.ZipFile(bundle_dir() / "payload.zip") as archive:
        return [name.replace("\\", "/") for name in archive.namelist() if not name.endswith("/")]


def clean_previous(target: Path, new_files: set[str], log: Callable[[str], None]) -> int:
    """删掉"上个版本装过、这个版本不再有"的文件。

    只相信 manifest 里记过的清单 —— 用户自己往目录里放的东西一概不碰。
    """
    manifest = target / MANIFEST_NAME
    try:
        previous = json.loads(manifest.read_text(encoding="utf-8"))
        old_files = {str(item).replace("\\", "/") for item in previous.get("files") or []}
    except (OSError, ValueError, AttributeError):
        old_files = set()
    if not old_files:
        log("没有上次安装的清单（首次安装或旧版安装包），只做覆盖")
        return 0

    removed = 0
    for relative in sorted(old_files - new_files):
        parts = Path(relative).parts
        if not parts or parts[0] in KEEP_ON_UPGRADE or ".." in parts:
            continue
        path = target / relative
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            continue
    # 顺手清掉因此变空的目录（深度优先，从最深的开始）
    for directory in sorted((item for item in target.rglob("*") if item.is_dir()),
                            key=lambda item: len(item.parts), reverse=True):
        relative = directory.relative_to(target).parts
        if not relative or relative[0] in KEEP_ON_UPGRADE:
            continue
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            continue
    if removed:
        log(f"清掉 {removed} 个上一版遗留的文件")
    return removed


def read_manifest(target: Path) -> dict:
    try:
        data = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_manifest(target: Path, files: set[str], requirements_hash: str) -> None:
    payload = {
        "version": bundle_version(),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": sorted(files),
        "requirements": requirements_hash,
    }
    (target / MANIFEST_NAME).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                        encoding="utf-8")


def bundle_version() -> str:
    try:
        return (bundle_dir() / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return "1.0.0"


def _requirements_hash(target: Path) -> str:
    import hashlib
    try:
        return hashlib.sha256((target / "requirements.txt").read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _registry_pythons() -> list[str]:
    """从注册表列出装过的 CPython；Python 安装器一定会写这里。"""
    if os.name != "nt":
        return []
    import winreg
    found: list[str] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Python\PythonCore", 0,
                                    winreg.KEY_READ | view) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        version = winreg.EnumKey(root, index)
                        try:
                            with winreg.OpenKey(root, version + r"\InstallPath") as key:
                                path, _ = winreg.QueryValueEx(key, "")
                                if path:
                                    found.append(str(Path(path) / "python.exe"))
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def _candidates() -> list[Path]:
    raw: list[str] = []
    if not getattr(sys, "frozen", False):
        raw.append(sys.executable)          # 已经在 Python 里跑，最省事
    raw += _registry_pythons()
    for name in ("python.exe", "python3.exe"):
        located = shutil.which(name)
        if located:
            raw.append(located)
    raw += [str(Path(root) / "python.exe") for root in
            (r"C:\Python313", r"C:\Python312", r"C:\Python311", r"C:\Python310")]

    result: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        # 商店版 python.exe 是个转 Store 的存根，一旦被调用会一直挂着等窗口。
        if not item or "windowsapps" in item.lower():
            continue
        path = Path(item)
        marker = str(path).casefold()
        if marker in seen or not path.is_file():
            continue
        seen.add(marker)
        result.append(path)
    # 挑底座：别家应用的虚拟环境排在最后（随时可能被删或改坏）；
    # 名字里带 python 的目录（python.org 的安装位置）排在最前，兼容性最好。
    def _rank(p: Path) -> int:
        parts = [part.casefold() for part in p.parts[:-1]]
        if any(part in (".venv", "venv", "env", "envs", "conda", "miniconda", "micromamba")
               for part in parts):
            return 2
        if any(part.startswith("python") for part in parts):
            return 0
        return 1
    return sorted(result, key=_rank)


def find_python() -> tuple[Path, str] | None:
    """找一个能建虚拟环境的 CPython 3.10+；每个候选最多等 10 秒。"""
    for path in _candidates():
        try:
            probe = subprocess.run(
                [str(path), "-c", "import sys;print('%d.%d' % sys.version_info[:2]);print(sys.executable)"],
                capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode != 0:
            continue
        lines = probe.stdout.decode("utf-8", "replace").strip().splitlines()
        if len(lines) < 2:
            continue
        major, _, minor = lines[-2].partition(".")
        try:
            if (int(major), int(minor)) < MIN_PYTHON:
                continue
        except ValueError:
            continue
        return Path(lines[-1]), lines[-2]
    return None


def shortcut_paths() -> tuple[Path, Path]:
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" / f"{APP_NAME}.lnk"
    start = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"
    return desktop, start


@dataclass
class Options:
    target: Path = field(default_factory=default_target)
    port: int = 8766
    desktop_shortcut: bool = True
    autostart: bool = True
    open_after: bool = True
    # 覆盖升级：停掉旧实例、按上一版清单精确清掉多余文件、依赖没变就不重装。
    upgrade: bool = False


# --------------------------------------------------------------------------- #
# 安装逻辑：GUI 和静默模式共用这一份
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, options: Options,
                 log: Callable[[str], None],
                 progress: Callable[[int, str], None]) -> None:
        self.options = options
        self.log = log
        self.progress = progress
        self.python: Path | None = None
        self.version = self._read_version()

    @staticmethod
    def _read_version() -> str:
        try:
            return (bundle_dir() / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0]
        except (OSError, IndexError):
            return "1.0.0"

    def _step(self, text: str, percent: int) -> None:
        self.progress(percent, text)
        self.log(text)

    def run(self) -> None:
        target = self.options.target
        payload = bundle_dir() / "payload.zip"
        if not payload.is_file():
            raise SetupError("安装包不完整：找不到 payload.zip。")

        self._step("检查运行环境…", 4)
        python = find_python()
        if not python:
            raise SetupError("这台电脑没有可用的 Python 3.10+。\n\n"
                             "请到 https://www.python.org/downloads/windows/ 下载安装，"
                             "安装时勾选 “Add python.exe to PATH”，然后重新运行安装向导。")
        python_exe, python_version = python
        self.python = python_exe
        self.log(f"Python {python_version}：{python_exe}")

        self._step("解开程序文件…", 12)
        target.mkdir(parents=True, exist_ok=True)
        # 有旧实例在跑就先请它退出：Windows 不允许覆盖正在执行的 .py/.exe。
        stop_running(target, self.log)
        members = payload_members()
        previous_requirements = read_manifest(target).get("requirements") or ""
        clean_previous(target, set(members), self.log)
        with zipfile.ZipFile(payload) as archive:
            archive.extractall(target)
        self.log(f"已解压到 {target}（{len(members)} 个文件）")

        exe_src = bundle_dir() / "Radar.exe"
        if exe_src.is_file():
            shutil.copy2(exe_src, target / "Radar.exe")
            self.log("已放入 Radar.exe（主程序入口）")

        self._step("创建虚拟环境…", 22)
        venv_dir = target / ".venv"
        venv_python = venv_dir / "Scripts" / "python.exe"
        if not venv_python.is_file():
            result = subprocess.run([str(python_exe), "-m", "venv", str(venv_dir)],
                                    capture_output=True, timeout=900)
            if result.returncode != 0:
                raise SetupError("创建虚拟环境失败：\n" + result.stderr.decode("utf-8", "replace")[-1500:])
        self.log(f"虚拟环境就绪：{venv_python}")

        self._step("升级 pip…", 28)
        subprocess.run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "-q"],
                       capture_output=True, timeout=900)

        current_requirements = _requirements_hash(target)
        requirements = target / "requirements.txt"
        last_error = ""
        if (previous_requirements and previous_requirements == current_requirements
                and self._deps_ok(venv_python)):
            # 只是换了程序代码、依赖一个字没动 —— 别再让用户等三分钟 pip。
            self._step("依赖没有变化，跳过安装", 70)
        else:
            self._step("安装依赖（最慢的一步，约 80 MB）…", 36)
            for index, extra in enumerate(([], ["-i", MIRROR]), 1):
                self.log(("用官方源" if index == 1 else "换清华镜像") + "安装依赖…")
                code = self._stream([str(venv_python), "-m", "pip", "install", "-r", str(requirements),
                                     "--disable-pip-version-check", *extra], timeout=5400)
                if code == 0:
                    break
                last_error = f"pip 返回 {code}"
                self.log(f"这一轮失败（{last_error}），换源重试。")
            else:
                raise SetupError("依赖安装失败。可以检查网络后重试，或手动运行：\n"
                                 f"{venv_python} -m pip install -r {requirements}\n最后错误：{last_error}")

            self._step("校验依赖…", 78)
            check = subprocess.run([str(venv_python), "-c", "import fastapi,uvicorn,bs4,lxml;print('ok')"],
                                   capture_output=True, timeout=180)
            if check.returncode != 0:
                raise SetupError("依赖装上了但导入失败：\n" + check.stderr.decode("utf-8", "replace")[-1200:])

        self._step("写入配置…", 84)
        self._write_settings(target)

        self._step("初始化数据库…", 88)
        init = subprocess.run(
            [str(venv_python), "-c",
             "import sys;sys.path.insert(0,'.');from app.db import Database;from app.paths import DB_PATH;"
             "Database(DB_PATH);print('db ready')"],
            cwd=str(target), capture_output=True, timeout=900)
        if init.returncode != 0:
            raise SetupError("数据库初始化失败：\n" + init.stderr.decode("utf-8", "replace")[-1500:])
        self.log("数据库就绪（已有资料不会被覆盖）")

        self._step("创建快捷方式…", 92)
        self._make_shortcuts(target, venv_python)

        self._step("登记卸载信息…", 96)
        self._write_uninstall_entry(target)

        if self.options.autostart:
            self._enable_autostart(target)
            self.log("已设为开机自动运行（可在设置页关掉）")

        # 清单最后写：中途失败时下次安装还能看到上一版的真实清单，清理才有依据。
        write_manifest(target, set(members), current_requirements)
        self._write_install_record(target)
        self._step("完成", 100)

        if self.options.open_after:
            self._relaunch(target)

    def _relaunch(self, target: Path) -> None:
        """升级完把新版本直接拉起来，用户不用再去点桌面图标。"""
        exe = target / "Radar.exe"
        if not exe.is_file():
            return
        try:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            subprocess.Popen([str(exe), "start"], cwd=str(target), close_fds=True,
                             creationflags=flags)
            self.log("已启动新版本")
        except OSError as exc:
            self.log(f"启动没成功（{exc}），双击桌面图标即可")

    def _deps_ok(self, venv_python: Path) -> bool:
        try:
            check = subprocess.run([str(venv_python), "-c",
                                    "import fastapi,uvicorn,bs4,lxml,httpx;print('ok')"],
                                   capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return False
        return check.returncode == 0

    # ---------------------------------------------------------------- 子步骤
    def _stream(self, command: list[str], timeout: float) -> int:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1)
        started = time.monotonic()
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                self.log("  " + line[:200])
            if time.monotonic() - started > timeout:
                process.kill()
                return 1
        return process.wait()

    def _write_settings(self, target: Path) -> None:
        path = target / "config" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            self.log("已有 settings.json，保留原设置，只更新端口")
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        else:
            example = target / "config" / "settings.example.json"
            data = json.loads(example.read_text(encoding="utf-8")) if example.is_file() else {"schema_version": 1}
        data["port"] = self.options.port
        data.setdefault("host", "127.0.0.1")
        data["schema_version"] = 1
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log(f"配置写好：{path}（端口 {self.options.port}）")

    def _make_shortcuts(self, target: Path, venv_python: Path) -> None:
        exe = target / "Radar.exe"
        if not exe.is_file():
            self.log("没有 Radar.exe，跳过快捷方式")
            return
        wanted: list[Path] = []
        desktop, start_menu = shortcut_paths()
        if self.options.desktop_shortcut:
            wanted.append(desktop)
        wanted.append(start_menu)
        code = f"""
import os
from win32com.client import Dispatch
shell = Dispatch("WScript.Shell")
for path in {[str(p) for p in wanted]!r}:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    link = shell.CreateShortCut(path)
    link.TargetPath = {str(exe)!r}
    link.WorkingDirectory = {str(target)!r}
    link.IconLocation = {str(exe)!r}
    link.Description = {APP_NAME!r}
    link.Save()
print("shortcuts ok")
"""
        result = subprocess.run([str(venv_python), "-c", code], capture_output=True, timeout=180)
        if result.returncode != 0:
            self.log("快捷方式创建失败（不影响使用）：" + result.stderr.decode("utf-8", "replace")[-300:])
        else:
            self.log("快捷方式已创建：" + "、".join(p.name for p in wanted))

    def _write_uninstall_entry(self, target: Path) -> None:
        import winreg
        exe = target / "Radar.exe"
        size = sum(item.stat().st_size for item in target.rglob("*") if item.is_file()) // 1024
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, self.version)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(target))
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(exe))
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{exe}" uninstall')
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, size)
        self.log("已登记到「应用和功能」，可从那里卸载")

    def _enable_autostart(self, target: Path) -> None:
        import winreg
        exe = target / "Radar.exe"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, f'"{exe}" start')
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        for name in ("Start Radar.vbs.lnk", "Radar.lnk"):
            try:
                (startup / name).unlink()
            except OSError:
                pass

    def _write_install_record(self, target: Path) -> None:
        record = {
            "version": self.version,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            # 记本实例自己虚拟环境里的解释器；self.python 只是建环境时借用的底座。
            "python": str(target / ".venv" / "Scripts" / "python.exe"),
            "base_python": str(self.python or ""),
            "port": self.options.port,
            "desktop_shortcut": self.options.desktop_shortcut,
            "autostart": self.options.autostart,
        }
        (target / "install.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #
class SetupApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} 安装向导")
        self.geometry("760x600")
        self.minsize(720, 560)
        self.configure(bg="#f4f6f8")
        self.target = tk.StringVar(value=str(default_target()))
        record = read_record(Path(self.target.get()))
        self.record = record
        # 装过、而且目录里确实有程序文件 → 走"覆盖更新"，把选择项锁死成上次的值。
        self.upgrade_mode = bool(record) and (Path(self.target.get()) / "app" / "api.py").is_file()
        self.port = tk.StringVar(value=str(record.get("port") or 8766))
        self.make_desktop = tk.BooleanVar(value=bool(record.get("desktop_shortcut", True)))
        self.make_autostart = tk.BooleanVar(value=bool(record.get("autostart", True)))
        self.open_after = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="")
        self.messages: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.options = Options()
        self.failed = False
        self._build_style()
        self.body = tk.Frame(self, bg="#f4f6f8")
        self.body.pack(fill="both", expand=True)
        self.pages: dict[str, tk.Frame] = {}
        self._build_welcome()
        self._build_progress()
        self._build_done()
        self.show("welcome")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Body.TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("TFrame", background="#f4f6f8")
        style.configure("TLabel", background="#f4f6f8")

    def _page(self, name: str) -> tk.Frame:
        frame = tk.Frame(self.body, bg="#f4f6f8", padx=34, pady=26)
        self.pages[name] = frame
        return frame

    def _build_welcome(self) -> None:
        page = self._page("welcome")
        if self.upgrade_mode:
            ttk.Label(page, text=f"更新到 {bundle_version()}", style="Title.TLabel").pack(anchor="w")
            ttk.Label(page, text="检测到已经装过一份。这次在原位置覆盖更新，资料库、设置、密钥都会保留。",
                      style="Body.TLabel").pack(anchor="w", pady=(6, 18))
        else:
            ttk.Label(page, text=f"{APP_NAME} 安装向导", style="Title.TLabel").pack(anchor="w")
            ttk.Label(page, text="把公众号和 B 站的新内容自动收进一个本地资料库，精读、归档、按需汇报。",
                      style="Body.TLabel").pack(anchor="w", pady=(6, 18))

        box = tk.LabelFrame(page, text=" 安装位置 ", bg="#ffffff", fg="#3d4a57", padx=14, pady=12,
                            font=("Microsoft YaHei UI", 9))
        box.pack(fill="x")
        row = tk.Frame(box, bg="#ffffff")
        row.pack(fill="x")
        entry = tk.Entry(row, textvariable=self.target, font=("Consolas", 9))
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        browse = ttk.Button(row, text="浏览…", command=self._browse)
        browse.pack(side="left", padx=(8, 0))
        if self.upgrade_mode:
            entry.configure(state="readonly")
            browse.state(["disabled"])
        tk.Label(box, text=("沿用上次的安装目录，不另建第二份。" if self.upgrade_mode
                            else "默认装在当前用户目录，不需要管理员权限。资料库（data）也在这里，别选 Program Files。"),
                 bg="#ffffff", fg="#697482", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(8, 0))

        options = tk.LabelFrame(page, text=" 选项 ", bg="#ffffff", fg="#3d4a57", padx=14, pady=12,
                                font=("Microsoft YaHei UI", 9))
        options.pack(fill="x", pady=14)
        line = tk.Frame(options, bg="#ffffff")
        line.pack(fill="x")
        tk.Label(line, text="网页端口", bg="#ffffff", font=("Microsoft YaHei UI", 9)).pack(side="left")
        spin = tk.Spinbox(line, from_=1024, to=65535, width=8, textvariable=self.port,
                          font=("Consolas", 9))
        spin.pack(side="left", padx=(8, 18))
        for text, var in (("创建桌面快捷方式", self.make_desktop),
                          ("开机自动运行", self.make_autostart),
                          ("装完立刻启动", self.open_after)):
            tk.Checkbutton(options, text=text, variable=var, bg="#ffffff",
                           font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(6, 0))
        if self.upgrade_mode:
            spin.configure(state="disabled")

        if self.upgrade_mode:
            tk.Label(page, bg="#e8f5ec", fg="#1c5c36", justify="left", padx=12, pady=10,
                     font=("Microsoft YaHei UI", 9),
                     text=f"上次装的版本：{self.record.get('version') or '未知'}\n"
                          "依赖没变的话几秒就装完，不会重新下载那 80 MB。").pack(fill="x")
        else:
            tk.Label(page, bg="#fff8e6", fg="#6b4c00", justify="left", padx=12, pady=10,
                     font=("Microsoft YaHei UI", 9),
                     text="安装会下载依赖（约 80 MB），第一次需要几分钟。\n"
                          "需要 Python 3.10 或更新版本；没有的话安装向导会给出下载链接。").pack(fill="x")

        buttons = tk.Frame(page, bg="#f4f6f8")
        buttons.pack(side="bottom", fill="x", pady=(16, 0))
        ttk.Button(buttons, text="退出", command=self._on_close).pack(side="right")
        ttk.Button(buttons, text="开始更新" if self.upgrade_mode else "开始安装", style="Primary.TButton",
                   command=self._confirm_install).pack(side="right", padx=(0, 8))

    def _build_progress(self) -> None:
        page = self._page("progress")
        ttk.Label(page, text="正在安装", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, textvariable=self.status, style="Body.TLabel").pack(anchor="w", pady=(6, 10))
        self.bar = ttk.Progressbar(page, mode="determinate", maximum=100)
        self.bar.pack(fill="x")
        wrap = tk.Frame(page, bg="#ffffff", highlightbackground="#dde3e9", highlightthickness=1)
        wrap.pack(fill="both", expand=True, pady=12)
        self.log = tk.Text(wrap, height=18, bg="#ffffff", bd=0, font=("Consolas", 9),
                           wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=8)
        self.cancel_button = ttk.Button(page, text="取消", command=self._on_close)
        self.cancel_button.pack(side="right")

    def _build_done(self) -> None:
        page = self._page("done")
        ttk.Label(page, text="装好了", style="Title.TLabel").pack(anchor="w")
        self.done_text = ttk.Label(page, text="", style="Body.TLabel", justify="left")
        self.done_text.pack(anchor="w", pady=(8, 16))
        buttons = tk.Frame(page, bg="#f4f6f8")
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="关闭", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="打开安装目录", command=self._open_target).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="启动雷达", style="Primary.TButton",
                   command=self._launch).pack(side="right", padx=(0, 8))

    def show(self, name: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)

    # --------------------------------------------------------------- 交互
    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title="选择安装目录", initialdir=str(default_target().parent))
        if not chosen:
            return
        target = Path(chosen)
        if target.name.lower() != APP_ID.lower():
            target = target / APP_ID
        self.target.set(str(target))

    def _confirm_install(self) -> None:
        target = Path(self.target.get().strip() or default_target())
        try:
            port = int(self.port.get())
            if not (1024 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "端口要是 1024 到 65535 之间的数字。", parent=self)
            return
        if "program files" in str(target).lower():
            if not messagebox.askyesno(APP_NAME, "装到 Program Files 需要管理员权限，资料库也写不进去。\n"
                                                 "建议换个目录，仍要继续吗？", parent=self):
                return
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"这个目录写不进去：{exc}\n换一个位置试试。", parent=self)
            return
        if (target / "app" / "api.py").is_file() and not self.upgrade_mode:
            if not messagebox.askyesno(APP_NAME, "这个目录里已经有一份雷达。\n"
                                                 "继续会更新程序文件，保留 data 目录和现有设置。要继续吗？", parent=self):
                return
        self.options = Options(target=target, port=port,
                               desktop_shortcut=self.make_desktop.get(),
                               autostart=self.make_autostart.get(),
                               open_after=self.open_after.get(),
                               upgrade=(target / "app" / "api.py").is_file())
        self.show("progress")
        self.cancel_button.state(["disabled"])
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(120, self._drain)

    def _worker(self) -> None:
        engine = Engine(self.options,
                        log=lambda text: self.messages.put(("log", text)),
                        progress=lambda percent, text: (self.messages.put(("bar", str(percent))),
                                                        self.messages.put(("status", f"{percent}% · {text}"))))
        try:
            engine.run()
            self.messages.put(("done", ""))
        except SetupError as exc:
            self.messages.put(("error", str(exc)))
        except Exception:
            self.messages.put(("error", traceback.format_exc()))

    def _drain(self) -> None:
        finished = False
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", payload + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "status":
                    self.status.set("正在安装… " + payload)
                elif kind == "bar":
                    self.bar["value"] = float(payload)
                elif kind == "done":
                    finished = True
                    self._finish(True)
                elif kind == "error":
                    finished = True
                    self._finish(False, payload)
        except queue.Empty:
            pass
        if not finished:
            self.after(120, self._drain)

    def _finish(self, ok: bool, detail: str = "") -> None:
        if ok:
            self.bar["value"] = 100
            self.status.set("完成")
            url = f"http://127.0.0.1:{self.options.port}"
            self.done_text.configure(text=f"程序目录：{self.options.target}\n"
                                          f"资料库：{self.options.target / 'data'}\n"
                                          f"打开地址：{url}\n\n"
                                          "第一次使用时，网页会从上到下引导你连接微信、接入大模型、选择汇报方式。")
            self.show("done")
            if self.options.open_after:
                self._launch()
        else:
            self.failed = True
            self.status.set("安装失败")
            self.log.configure(state="normal")
            self.log.insert("end", "\n" + detail + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
            self.cancel_button.state(["!disabled"])
            self.cancel_button.configure(text="关闭")
            messagebox.showerror(APP_NAME, "安装没有完成。\n下面的日志里有具体原因，可以截图发给我。", parent=self)

    def _launch(self) -> None:
        exe = self.options.target / "Radar.exe"
        if not exe.is_file():
            messagebox.showwarning(APP_NAME, "找不到 Radar.exe，可能安装没完成。", parent=self)
            return
        subprocess.Popen([str(exe), "start"], cwd=str(self.options.target))
        self.after(1500, lambda: __import__("webbrowser").open(f"http://127.0.0.1:{self.options.port}"))

    def _open_target(self) -> None:
        try:
            os.startfile(str(self.options.target))  # noqa: S606
        except OSError:
            pass

    def _on_close(self) -> None:
        if self.pages["progress"].winfo_ismapped() and not self.pages["done"].winfo_ismapped() and not self.failed:
            if not messagebox.askyesno(APP_NAME, "安装还在进行，现在退出可能留下半装状态。确定退出吗？", parent=self):
                return
        self.destroy()


# --------------------------------------------------------------------------- #
# 静默安装
# --------------------------------------------------------------------------- #
class _Tee:
    """把日志同时写到文件和原来的输出。

    装到一半失败时，用户手里只有"双击了没反应"，能拿到一份日志就能定位。
    所以凡是打包后的静默安装，一律留痕。
    """

    def __init__(self, path: Path, mirror) -> None:
        self._file = open(path, "w", encoding="utf-8", buffering=1)
        self._mirror = mirror

    def write(self, text: str) -> int:
        self._file.write(text)
        if self._mirror is not None:
            try:
                self._mirror.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        self._file.flush()
        if self._mirror is not None:
            try:
                self._mirror.flush()
            except Exception:
                pass


def silent_install(args: list[str]) -> int:
    options = Options()
    # 第一遍只认目录，好据此读出上次装的端口和选项
    for arg in args:
        if arg.lower().startswith("--dir="):
            options.target = Path(arg.split("=", 1)[1]).expanduser().resolve()
    record = read_record(options.target)
    if record:
        options.upgrade = True
        try:
            options.port = int(record.get("port") or options.port)
        except (TypeError, ValueError):
            pass
        options.desktop_shortcut = bool(record.get("desktop_shortcut", True))
        options.autostart = bool(record.get("autostart", True))

    # 第二遍：命令行显式给的覆盖掉上面的推断
    for arg in args:
        lower = arg.lower()
        if lower.startswith("--dir="):
            continue
        if lower.startswith("--port="):
            options.port = int(arg.split("=", 1)[1])
        elif lower == "--no-desktop":
            options.desktop_shortcut = False
        elif lower == "--no-autostart":
            options.autostart = False
        elif lower == "--no-launch":
            options.open_after = False
        elif lower == "--launch":
            options.open_after = True
        elif lower == "--upgrade":
            options.upgrade = True
    if getattr(sys, "frozen", False):
        import tempfile
        log_path = Path(tempfile.gettempdir()) / "radarsetup-silent.log"
        sys.stdout = sys.stderr = _Tee(log_path, sys.stdout)
        print(f"日志：{log_path}")
    print(f"{'覆盖更新' if options.upgrade else '静默安装'}到 {options.target}（端口 {options.port}）")
    engine = Engine(options, log=print, progress=lambda percent, text: None)
    try:
        engine.run()
    except SetupError as exc:
        print("安装失败：" + str(exc), file=sys.stderr)
        return 1
    print("安装完成。启动命令：" + str(options.target / "Radar.exe") + " start")
    return 0


def main(argv: list[str]) -> int:
    if os.name != "nt":
        print("这个安装向导只支持 Windows。")
        return 2
    if "/S" in argv or "--silent" in argv:
        return silent_install(argv[1:])
    app = SetupApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
