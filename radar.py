#!/usr/bin/env python
"""Hermes 科研雷达 —— 一个入口管所有事情。

    python radar.py status              现在改了什么
    python radar.py save "修好图注"      存一个版本
    python radar.py undo                退回上一个版本
    python radar.py list                看过往版本
    python radar.py check               语法 + 测试
    python radar.py start / stop        启停服务
    python radar.py pack                打成可分发的压缩包
    python radar.py tidy                清掉临时文件，并把回收站里本项目的东西直删
    python radar.py release "改了什么"   发一个新版本到 GitHub（打 tag + 传安装包）
    python radar.py where               报一下各目录在哪

不需要懂 git。所有会覆盖改动的操作都会先自动留底。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tools import packaging, vcs  # noqa: E402
from tools.vcs import Repo, VcsError, python_exe  # noqa: E402


def _rule(title: str = "") -> None:
    print(f"\n{title}" if title else "")


def cmd_status(repo: Repo) -> int:
    print(repo.summary())
    return 0


def cmd_save(repo: Repo, message: str) -> int:
    if not message.strip():
        print("要给这次改动写一句话，比如：python radar.py save \"修好图注截断\"")
        return 2
    created = repo.ensure()
    if created:
        print("首次建库完成，已把当前代码当作起点。")
    pending = repo.pending()
    if not pending:
        print("没有改动可存 —— 现在的代码和上一次保存一模一样。")
        return 0
    stat = repo.diff_stat()
    entry = repo.save(message)
    if entry is None:
        print("没有改动可存。")
        return 0
    print(f"已保存：{entry.subject}  ({entry.short}  {entry.when})")
    if stat:
        for line in stat.splitlines()[-3:]:
            print("  " + line.strip())
    print(f"  本次涉及 {len(pending)} 个文件")
    return 0


def cmd_list(repo: Repo, count: int, include_autosave: bool = False) -> int:
    entries = repo.history(count, include_autosave=include_autosave)
    if not entries:
        print("还没有保存过任何版本。运行 `radar.py save \"说明\"` 开始。")
        return 0
    print(f"{'编号':<4} {'时间':<17} {'版本':<8} 说明")
    print("-" * 78)
    for index, entry in enumerate(entries, 1):
        version = entry.version or "-"
        subject = entry.subject
        if entry.autosave:
            subject = "[回滚留底] " + subject
        print(f"{index:<5} {entry.when:<17} {version:<9} {subject}")
    print("-" * 78)
    print("退回某个版本：radar.py back <编号>      只看最近 N 个：radar.py list 50")
    return 0


def cmd_undo(repo: Repo) -> int:
    """两种"撤销"，按当前状态自动选，不用使用者自己判断：
    有未保存的改动 → 丢掉改动，回到上一次保存的样子；
    工作区干净     → 退回再上一个已保存的版本。
    """
    if not repo.has_commits():
        print("还没有保存过任何版本，没得回滚。")
        return 2
    pending = repo.pending()
    if not pending:
        entries = repo.history(2)
        if len(entries) < 2:
            print("现在只有一个版本（刚建的起点），没有上一个版本可退。")
            return 2
        target = entries[1]
        print(f"准备退回到上一个版本：{target.when}  {target.subject}  ({target.short})")
        tag, sha = repo.rollback(target.sha, reason="undo")
        print(f"已退回，代码和配置现在是 {sha[:8]} 的样子。")
    else:
        head = repo.head_sha()
        print(f"有 {len(pending)} 个文件改了还没保存，先丢掉这些改动，回到上一次保存的样子。")
        tag, sha = repo.rollback(head, reason="丢弃未保存改动")
        print(f"已回到 {sha[:8]}。")
    if tag:
        print(f"回滚前的状态已留底为 {tag}")
        print(f"  想反悔：radar.py back {tag}")
    print("记得重启服务：radar.py stop，再 radar.py start")
    return 0


def cmd_back(repo: Repo, revision: str) -> int:
    resolved = repo.resolve(revision)
    current = repo.head_sha()
    if resolved == current and not repo.pending():
        print("现在就在这个版本上。")
        return 0
    tag, sha = repo.rollback(revision, reason=f"back {revision}")
    print(f"已切换到 {sha[:8]}。")
    if tag:
        print(f"切换前的状态已留底为 {tag}，想回去就：radar.py back {tag}")
    print("记得重启服务：radar.py stop && radar.py start")
    return 0


def cmd_config_restore(repo: Repo) -> int:
    try:
        repo.restore_config()
    except VcsError as exc:
        print(f"失败：{exc}")
        return 2
    print("已把 config/settings.json 恢复成上次保存的样子。")
    print("服务如果开着，需要 radar.py stop && radar.py start 才生效。")
    return 0


def cmd_check(repo: Repo, full: bool) -> int:
    print("检查语法 …")
    ok, output = repo.compile_check()
    print("  通过" if ok else "  失败")
    if not ok:
        print(output)
        return 1
    if not full:
        print("跑单元测试：radar.py check --full")
        return 0
    print("跑单元测试（大约 1 分钟）…")
    started = time.time()
    ok, output = repo.run_tests()
    elapsed = time.time() - started
    tail = output.strip().splitlines()[-1] if output.strip() else ""
    print(f"  {'通过' if ok else '失败'}  用时 {elapsed:.0f}s")
    if not ok:
        print(output[-3000:])
        return 1
    print(f"  {tail}")
    return 0


def cmd_tag(repo: Repo, name: str) -> int:
    if not name:
        print("用法：radar.py tag v1.0")
        return 2
    repo.tag_version(name)
    print(f"已给当前版本打上标记 {name}。以后可以 radar.py back {name}")
    return 0


def cmd_start(repo: Repo) -> int:
    if not (ROOT / ".venv").is_dir():
        print("还没有安装运行环境。先运行：python install.py")
        return 2
    stop_file = ROOT / "data" / "STOP_RADAR"
    if stop_file.exists():
        stop_file.unlink()
    supervisor = ROOT / "supervisor.pyw"
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.is_file():
        pythonw = python_exe()
    subprocess.Popen([str(pythonw), str(supervisor)], cwd=str(ROOT),
                     creationflags=0x00000008)  # DETACHED_PROCESS
    print("已后台启动。等 5 秒左右，然后打开 http://127.0.0.1:8766")
    return 0


def cmd_stop() -> int:
    stop_file = ROOT / "data" / "STOP_RADAR"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text("stop", encoding="utf-8")
    print("已发出停止信号。窗口和后台进程会在几秒内退出。")
    return 0


def cmd_pack(repo: Repo, output: str) -> int:
    path = Path(output).expanduser().resolve() if output else None
    result = packaging.build(path, ROOT)
    print(packaging.describe(result))
    return 0


def cmd_tidy() -> int:
    """清掉本项目的临时产物，再把回收站里属于本项目的条目直删。

    为什么要单独一条命令：这台机器（以及任何装"安全删除"垫片的环境）会把
    `shutil.rmtree` 换成"移入回收站"。开发一晚上就能在回收站里堆出上千条
    `test.db-shm`，删东西还会越来越慢。这里把两件事合起来做：
    先删临时目录，再把回收站里属于本项目的记录直删，用户自己删的东西不碰。
    """
    from app.paths import BASE_DIR, DATA_DIR
    from app.services import purge

    targets = [BASE_DIR / ".tmp", BASE_DIR / ".tmpdata", DATA_DIR / ".tmp",
               DATA_DIR / "wechat-self" / "__pycache__"]
    total = 0
    for path in targets:
        if not path.exists():
            continue
        size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        purge.remove(path)
        total += size
        print(f"已删 {path}  ({size / 1024 / 1024:.1f} MB)")
    print(f"临时文件合计 {total / 1024 / 1024:.1f} MB")

    python = python_exe()
    for prefix in (str(BASE_DIR), str(DATA_DIR)):
        result = subprocess.run([python, str(ROOT / "tools" / "trash.py"), "purge", "--under", prefix],
                                text=True, encoding="utf-8", errors="replace", capture_output=True)
        tail = [line for line in (result.stdout or "").strip().splitlines() if line][-2:]
        print("\n".join(tail) or "（回收站里没有本项目的记录）")
    return 0


def cmd_where() -> int:
    settings = ROOT / "config" / "settings.json"
    print(f"程序目录  {ROOT}")
    print(f"数据目录  {ROOT / 'data'}")
    print(f"配置文件  {settings}  {'存在' if settings.is_file() else '不存在（用默认值）'}")
    print(f"版本库    {ROOT / '.git'}  {'存在' if (ROOT / '.git').exists() else '未建立'}")
    print(f"项目版本  {vcs.project_version()}")
    try:
        sys.path.insert(0, str(ROOT))
        from app.db import Database
        from app.paths import DB_PATH
        info = Database(DB_PATH).schema_info()
        drift = "" if info["file_version"] == info["code_version"] else "  ← 和代码版本不一致"
        print(f"数据库    第 {info['file_version']} 版（代码要求 {info['code_version']}），"
              f"{info['size_mb']} MB，{info['tables']} 张表{drift}")
    except Exception as exc:
        print(f"数据库    读不出来：{type(exc).__name__}: {exc}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help", "?"):
        print(__doc__)
        print(vcs.long_help())
        return 0
    command, rest = argv[0], argv[1:]
    repo = Repo(ROOT)
    try:
        if command in ("status", "st"):
            return cmd_status(repo)
        if command in ("save", "commit", "ci"):
            return cmd_save(repo, " ".join(rest))
        if command in ("list", "log", "ls"):
            count = int(rest[0]) if rest and rest[0].isdigit() else 20
            return cmd_list(repo, count, include_autosave="--all" in rest)
        if command in ("undo", "revert", "回来"):
            return cmd_undo(repo)
        if command in ("back", "restore", "go"):
            if not rest:
                print("用法：radar.py back <编号>   （编号从 radar.py list 里看）")
                return 2
            return cmd_back(repo, rest[0])
        if command in ("config-restore", "config"):
            return cmd_config_restore(repo)
        if command in ("check", "test"):
            return cmd_check(repo, full="--full" in rest)
        if command == "tag":
            return cmd_tag(repo, rest[0] if rest else "")
        if command == "start":
            return cmd_start(repo)
        if command in ("stop", "quit"):
            return cmd_stop()
        if command in ("tidy", "clean"):
            return cmd_tidy()
        if command in ("pack", "build"):
            output = rest[0] if rest else ""
            return cmd_pack(repo, output)
        if command in ("release", "publish", "发布"):
            from tools import release
            return release.main(rest)
        if command in ("where", "info"):
            return cmd_where()
        print(f"不认识这个命令：{command}\n")
        print(__doc__)
        return 2
    except VcsError as exc:
        print(f"出错了：{exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
