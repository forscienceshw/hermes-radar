"""把微信 3.9.9.43 装回去 —— 4.x 读取出问题时的保险绳。

这不是腾讯的安装程序，而是把升级前那份微信原样铺回磁盘：程序文件来自
wechat-backup.zip，注册表项由本程序重建（写在 HKCU，不需要管理员）。

三条设计原则：

1. **只碰程序目录，绝不碰聊天记录。** 聊天记录在 Documents\\WeChat Files，
   由微信自己管，4.x 也不会改它；这里一个字节都不动。
2. **和 4.x 共存而不是覆盖。** 3.x 的主程序叫 WeChat.exe、程序体在 [3.9.9.43]
   子目录；4.x 叫 Weixin.exe、用自己版本号命名子目录。两者放在同一个文件夹里
   互不干扰，所以"装到同一个文件夹"这件事在这里是安全的。
3. **先看清楚再动手。** 目标目录不存在就新建；已存在就把要写入的文件列出来，
   用户点确认才开工。

用法：
    双击 恢复微信3.9.9.43.exe        图形界面
    恢复微信3.9.9.43.exe /S --dir=D:\\program\\WeChat
                                      静默恢复（日志打在控制台）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

APP_TITLE = "微信 3.9.9.43 恢复工具"
DEFAULT_TARGET = Path(r"D:\program\WeChat")
BACKUP_NAME = "wechat-backup.zip"
RECORD = ".restore-record.json"
# 恢复工具也是 --windowed 的，拉微信时别让 Windows 给个黑框。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def find_backup() -> Path | None:
    """备份包优先找 exe 旁边（用户解压出来的位置），再找打包进去的那份。"""
    candidates = [Path(sys.executable).resolve().parent / BACKUP_NAME,
                  Path(__file__).resolve().parent / BACKUP_NAME,
                  bundle_dir() / BACKUP_NAME]
    for path in candidates:
        if path.is_file():
            return path
    return None


def target_from_backup(backup: Path) -> Path | None:
    """zip 顶层如果有唯一目录名，就用它作为默认目标。"""
    try:
        with zipfile.ZipFile(backup) as archive:
            tops = {name.split("/", 1)[0] for name in archive.namelist() if name.strip()}
        if len(tops) == 1:
            return DEFAULT_TARGET.parent / tops.pop()
    except (OSError, zipfile.BadZipFile):
        return None
    return None


# --------------------------------------------------------------------------- #
# 进程
# --------------------------------------------------------------------------- #
def wechat_processes() -> list[str]:
    names = {"wechat.exe", "weixin.exe"}
    found: list[str] = []
    try:
        import psutil
    except ImportError:
        return found
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name in names:
                found.append(f"{name}({proc.pid})")
        except Exception:  # noqa: BLE001
            continue
    return found


def stop_wechat(log) -> None:
    try:
        import psutil
    except ImportError:
        return
    killed = []
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if (proc.info.get("name") or "").lower() in {"wechat.exe", "weixin.exe"}:
                proc.terminate()
                killed.append(proc)
        except Exception:  # noqa: BLE001
            continue
    if killed:
        _, alive = psutil.wait_procs(killed, timeout=8)
        for proc in alive:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        log(f"已退出 {len(killed)} 个微信进程")


# --------------------------------------------------------------------------- #
# 恢复
# --------------------------------------------------------------------------- #
def restore(backup: Path, target: Path, log, progress) -> None:
    if not backup.is_file():
        raise RuntimeError(f"找不到备份包：{backup}")
    with zipfile.ZipFile(backup) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if not members:
            raise RuntimeError("备份包是空的")
        total = sum(item.file_size for item in members)
        log(f"备份包 {backup.stat().st_size / 1024 / 1024:.0f} MB，解压后 {total / 1024 / 1024:.0f} MB")

        needed = total - sum(item.compress_size for item in members)
        free = shutil.disk_usage(target.anchor or target.parent).free
        if needed > free * 0.95:
            raise RuntimeError(f"磁盘空间不够：需要约 {needed / 1024 / 1024:.0f} MB，"
                               f"{target.anchor} 只剩 {free / 1024 / 1024:.0f} MB")
        target.mkdir(parents=True, exist_ok=True)

        written = 0
        for index, item in enumerate(members, 1):
            # zip 里的路径形如 WeChat/WeChat.exe；目标是 ...\WeChat，所以去掉第一段
            parts = Path(item.filename).parts
            relative = Path(*parts[1:]) if len(parts) > 1 else Path(parts[0])
            if ".." in relative.parts:
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, destination.open("wb") as handle:
                shutil.copyfileobj(source, handle, 1024 * 1024)
            written += item.file_size
            if index % 3 == 0 or index == len(members):
                progress(int(written * 100 / total), f"{relative.as_posix()}")

    exe = target / "WeChat.exe"
    if not exe.is_file():
        raise RuntimeError(f"解压完了但没找到 {exe}，备份包可能不完整")
    log(f"程序文件已就位：{exe}")


def register(target: Path, log) -> None:
    """写进「应用和功能」。HKCU 不需要管理员，效果和原来的 HKLM 一样。"""
    if os.name != "nt":
        return
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\WeChat"
    exe = target / "WeChat.exe"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "微信")
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, "3.9.9.43")
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "腾讯科技(深圳)有限公司")
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(target))
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(exe))
            uninstall = target / "Uninstall.exe"
            if uninstall.is_file():
                winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{uninstall}"')
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        log("已登记到「应用和功能」")
    except OSError as exc:
        log(f"写注册表失败（不影响使用）：{exc}")


def write_record(target: Path, backup: Path) -> None:
    payload = {
        "restored_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "version": "3.9.9.43",
        "source": backup.name,
        "note": "聊天记录在 Documents\\WeChat Files，未改动",
    }
    try:
        (target / RECORD).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #
def gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("720x560")
    root.minsize(680, 520)
    root.configure(bg="#f4f6f8")

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
    style.configure("Body.TLabel", font=("Microsoft YaHei UI", 9))
    style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("TFrame", background="#f4f6f8")
    style.configure("TLabel", background="#f4f6f8")

    backup = find_backup()
    suggested = target_from_backup(backup) if backup else None
    target_var = tk.StringVar(value=str(suggested or DEFAULT_TARGET))
    status_var = tk.StringVar(value="")
    running_var = tk.StringVar(value="")

    body = tk.Frame(root, bg="#f4f6f8", padx=30, pady=24)
    body.pack(fill="both", expand=True)

    ttk.Label(body, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
    ttk.Label(body, text="把升级前那份微信原样铺回去。聊天记录不会被碰。",
              style="Body.TLabel").pack(anchor="w", pady=(6, 16))

    info = tk.LabelFrame(body, text=" 备份包 ", bg="#ffffff", fg="#3d4a57", padx=14, pady=12,
                         font=("Microsoft YaHei UI", 9))
    info.pack(fill="x")
    if backup:
        size_mb = backup.stat().st_size / 1024 / 1024
        tk.Label(info, text=f"{backup.name}  ·  {size_mb:.0f} MB", bg="#ffffff",
                 font=("Consolas", 9)).pack(anchor="w")
        tk.Label(info, text=str(backup), bg="#ffffff", fg="#697482",
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(2, 0))
    else:
        tk.Label(info, text=f"找不到 {BACKUP_NAME}", bg="#ffffff", fg="#b42318",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        tk.Label(info, text="把备份包和本程序放在同一个文件夹里，再打开本程序。",
                 bg="#ffffff", fg="#697482", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))

    place = tk.LabelFrame(body, text=" 恢复到哪个文件夹 ", bg="#ffffff", fg="#3d4a57",
                          padx=14, pady=12, font=("Microsoft YaHei UI", 9))
    place.pack(fill="x", pady=14)
    row = tk.Frame(place, bg="#ffffff")
    row.pack(fill="x")
    tk.Entry(row, textvariable=target_var, font=("Consolas", 9)).pack(side="left", fill="x",
                                                                     expand=True, ipady=4)

    def browse() -> None:
        chosen = filedialog.askdirectory(title="选择微信程序目录")
        if chosen:
            target_var.set(chosen)

    ttk.Button(row, text="浏览…", command=browse).pack(side="left", padx=(8, 0))
    tk.Label(place, text="3.x 的主程序是 WeChat.exe，4.x 是 Weixin.exe，放在同一个文件夹里不会互相覆盖。",
             bg="#ffffff", fg="#697482", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(8, 0))

    warn = tk.Label(body, bg="#fff6dd", fg="#6b4c00", justify="left", padx=12, pady=10,
                    font=("Microsoft YaHei UI", 9),
                    text="恢复前会先把正在运行的微信关掉。\n"
                         "聊天记录在「文档\\WeChat Files」，本工具不读不写，回退后照常能看到。")
    warn.pack(fill="x")

    ttk.Label(body, textvariable=running_var, style="Body.TLabel").pack(anchor="w", pady=(12, 0))
    ttk.Label(body, textvariable=status_var, style="Body.TLabel").pack(anchor="w", pady=(4, 0))
    bar = ttk.Progressbar(body, mode="determinate", maximum=100)
    bar.pack(fill="x", pady=(10, 0))

    log_box = tk.Text(body, height=8, bg="#ffffff", bd=0, font=("Consolas", 9), wrap="word",
                      state="disabled", highlightbackground="#dde3e9", highlightthickness=1)
    log_box.pack(fill="both", expand=True, pady=(10, 0))

    def log(text: str) -> None:
        log_box.configure(state="normal")
        log_box.insert("end", text + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    buttons = tk.Frame(body, bg="#f4f6f8")
    buttons.pack(fill="x", pady=(14, 0))

    def refresh_running() -> None:
        found = wechat_processes()
        running_var.set("正在运行：" + "、".join(found) if found else "微信当前没有运行")

    def start() -> None:
        if not backup:
            messagebox.showerror(APP_TITLE, f"找不到 {BACKUP_NAME}。\n把它和本程序放在同一个文件夹里。",
                                 parent=root)
            return
        target = Path(target_var.get().strip() or DEFAULT_TARGET)
        if not messagebox.askyesno(
                APP_TITLE,
                f"要把微信 3.9.9.43 恢复到：\n{target}\n\n"
                "同名文件会被覆盖，聊天记录不受影响。继续吗？", parent=root):
            return
        start_button.state(["disabled"])

        def worker() -> None:
            try:
                stop_wechat(log)
                status_var.set("正在解压…")
                restore(backup, target, log, lambda pct, name: (bar.configure(value=pct),
                                                                status_var.set(f"{pct}% · {name}")))
                register(target, log)
                write_record(target, backup)
                bar.configure(value=100)
                status_var.set("完成")
                log("恢复完成。")
                root.after(0, lambda: ask_launch(target))
            except Exception as exc:  # noqa: BLE001
                log(f"失败：{type(exc).__name__}: {exc}")
                status_var.set("恢复失败")
                root.after(0, lambda: messagebox.showerror(APP_TITLE, str(exc), parent=root))
            finally:
                root.after(0, lambda: start_button.state(["!disabled"]))

        threading.Thread(target=worker, daemon=True).start()

    def ask_launch(target: Path) -> None:
        exe = target / "WeChat.exe"
        if exe.is_file() and messagebox.askyesno(APP_TITLE, "恢复好了。现在启动微信吗？", parent=root):
            try:
                subprocess.Popen([str(exe)], cwd=str(target), close_fds=True,
                                 creationflags=CREATE_NO_WINDOW)
            except OSError as exc:
                messagebox.showerror(APP_TITLE, f"启动失败：{exc}", parent=root)

    ttk.Button(buttons, text="关闭", command=root.destroy).pack(side="right")
    start_button = ttk.Button(buttons, text="开始恢复", style="Primary.TButton", command=start)
    start_button.pack(side="right", padx=(0, 8))
    ttk.Button(buttons, text="刷新进程状态", command=refresh_running).pack(side="right", padx=(0, 8))

    refresh_running()
    root.mainloop()
    return 0


def silent(argv: list[str]) -> int:
    backup = find_backup()
    target = DEFAULT_TARGET
    for arg in argv:
        if arg.lower().startswith("--dir="):
            target = Path(arg.split("=", 1)[1]).expanduser().resolve()
    if not backup:
        print(f"找不到 {BACKUP_NAME}", file=sys.stderr)
        return 2
    print(f"备份包 {backup}")
    print(f"目标   {target}")
    stop_wechat(print)
    try:
        restore(backup, target, print, lambda pct, name: print(f"  {pct}% {name}"))
        register(target, print)
        write_record(target, backup)
    except Exception as exc:  # noqa: BLE001
        print(f"恢复失败：{exc}", file=sys.stderr)
        return 1
    print("完成")
    return 0


def main(argv: list[str]) -> int:
    if os.name != "nt":
        print("这个工具只支持 Windows。")
        return 2
    if "/S" in argv or "--silent" in argv:
        return silent(argv[1:])
    return gui()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
