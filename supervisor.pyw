import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
STOP = DATA / "STOP_RADAR"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
_LOCK_FILE = None


def mutex_available():
    """A cross-session file lock complements the per-session Windows mutex."""
    global _LOCK_FILE
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\HermesResearchRadarSupervisor")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return False, handle
    try:
        import msvcrt
        _LOCK_FILE = (DATA / "supervisor.lock").open("a+b")
        _LOCK_FILE.seek(0)
        if not _LOCK_FILE.read(1):
            _LOCK_FILE.write(b"0")
            _LOCK_FILE.flush()
        _LOCK_FILE.seek(0)
        msvcrt.locking(_LOCK_FILE.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        if _LOCK_FILE:
            _LOCK_FILE.close()
            _LOCK_FILE = None
        return False, handle
    return True, handle


def launch(module, log_file):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BASE)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("TEMP", str(BASE / ".tmp"))
    env.setdefault("TMP", str(BASE / ".tmp"))
    env.setdefault("XDG_CACHE_HOME", str(BASE / ".cache"))
    env.setdefault("HF_HOME", str(BASE / ".cache" / "huggingface"))
    log = open(log_file, "a", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", module], cwd=BASE, env=env, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
    log.close()
    return process


def ensure_wechat():
    """Optionally launch the installed client once; never bypass its login flow."""
    config_path = BASE / "config" / "startup.json"
    if not config_path.is_file():
        return
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not config.get("auto_launch_wechat"):
            return
        import psutil
        names = {(p.name() or "").lower() for p in psutil.process_iter(["name"])}
        if {"wechat.exe", "weixin.exe"} & names:
            return
        candidates = []
        configured = Path(config.get("wechat_exe") or "")
        if str(configured) != ".":
            candidates.append(configured)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat") as key:
                install, _ = winreg.QueryValueEx(key, "InstallPath")
                candidates.append(Path(install) / "WeChat.exe")
        except OSError:
            pass
        candidates.extend([Path("C:/Program Files/Tencent/WeChat/WeChat.exe"),
                           Path("C:/Program Files (x86)/Tencent/WeChat/WeChat.exe")])
        executable = next((path for path in candidates if path.is_file()), None)
        if executable:
            subprocess.Popen([str(executable)], cwd=str(executable.parent), close_fds=True)
            with (DATA / "logs" / "startup.log").open("a", encoding="utf-8") as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} launched WeChat\n")
    except Exception as exc:
        with (DATA / "logs" / "startup.log").open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} WeChat launch failed: {type(exc).__name__}\n")


def main():
    available, handle = mutex_available()
    if not available:
        return
    STOP.unlink(missing_ok=True)
    ensure_wechat()
    children = {"app.api": None, "app.worker": None}
    failures = {name: 0 for name in children}
    (DATA / "supervisor.json").write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}), encoding="utf-8")
    try:
        while not STOP.exists():
            for name, process in list(children.items()):
                if process is None or process.poll() is not None:
                    if process is not None:
                        failures[name] += 1
                    children[name] = launch(name, DATA / "logs" / (name.replace(".", "-") + "-console.log"))
                    time.sleep(min(30, 2 ** min(failures[name], 4)))
            time.sleep(2)
    finally:
        for process in children.values():
            if process and process.poll() is None:
                process.terminate()
        deadline = time.time() + 8
        for process in children.values():
            if process and process.poll() is None:
                try:
                    process.wait(timeout=max(1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    process.kill()
        if _LOCK_FILE:
            _LOCK_FILE.close()


if __name__ == "__main__":
    main()
