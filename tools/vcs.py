"""版本控制的底层实现：把 git 包成几个不容易用错的动作。

三条设计原则，都是为了"改坏了能退回去"这件事真的成立：

1. 任何会覆盖工作区的操作（undo / back），一定先把当前状态钉成一个 tag。
   所以"回滚错了"从来不是死路，最多是再 back 一次。
2. 数据目录 data/ 不在仓库里。回滚只动代码与配置，不会碰到资料库、图片和数据库。
3. 所有提示用中文，使用者不需要知道 git 是什么。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
AUTOSAVE_PREFIX = "自动快照"
# 自动快照是回滚动作的副产物，不该混在正常版本列表里干扰判断。
AUTOSAVE_TAG = "radar-autosave-"


class VcsError(RuntimeError):
    pass


def project_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def python_exe() -> Path:
    """优先用项目自带的虚拟环境，避免拿系统 Python 跑测试缺依赖。"""
    for candidate in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


@dataclass
class Entry:
    sha: str
    when: str
    subject: str
    version: str
    autosave: bool

    @property
    def short(self) -> str:
        return self.sha[:8]


class Repo:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or ROOT)

    # -- 基础 ---------------------------------------------------------------

    def git(self, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
        command = [
            "git",
            # quotepath=false 让中文文件名原样输出，不然满屏 \344\275\240 没法看。
            "-c", "core.quotepath=false",
            "-c", "core.autocrlf=false",
            *args,
        ]
        try:
            result = subprocess.run(
                command, cwd=str(self.root), capture_output=True, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"},
            )
        except FileNotFoundError as exc:
            raise VcsError("找不到 git。请先安装 Git for Windows 并确认 git 在 PATH 里。") from exc
        except subprocess.TimeoutExpired as exc:
            raise VcsError(f"git {' '.join(args)} 超时") from exc
        if check and result.returncode != 0:
            message = _decode(result.stderr).strip() or _decode(result.stdout).strip()
            raise VcsError(f"git {' '.join(args)} 失败：{message}")
        return result

    def text(self, *args: str, check: bool = True) -> str:
        return _decode(self.git(*args, check=check).stdout)

    def exists(self) -> bool:
        return (self.root / ".git").exists()

    def has_commits(self) -> bool:
        if not self.exists():
            return False
        return self.git("rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0

    def ensure(self) -> bool:
        """必要时建仓。返回 True 表示是这次新建的。"""
        created = False
        if not self.exists():
            self.git("init", "-b", "main")
            # 本地单人项目，关掉换行符改写，避免整文件假 diff。
            self.git("config", "core.autocrlf", "false")
            self.git("config", "i18n.commitEncoding", "utf-8")
            self.git("config", "i18n.logOutputEncoding", "utf-8")
            self.git("config", "core.quotepath", "false")
            created = True
        if not self.has_commits():
            self.git("add", "-A")
            self.git("commit", "-m", "初始版本：把现有代码固定下来",
                     "--no-verify")
            created = True
        return created

    # -- 查询 ---------------------------------------------------------------

    def pending(self) -> list[tuple[str, str]]:
        """工作区相对 HEAD 的改动，返回 [(状态, 路径)]。"""
        if not self.has_commits():
            return []
        raw = self.text("status", "--porcelain=v1", "--untracked-files=all")
        entries: list[tuple[str, str]] = []
        for line in raw.splitlines():
            if len(line) < 4:
                continue
            entries.append((line[:2].strip() or "?", line[3:].strip()))
        return entries

    def diff_stat(self) -> str:
        staged = self.text("diff", "--cached", "--stat", check=False).strip()
        worktree = self.text("diff", "--stat", check=False).strip()
        parts = [part for part in (staged, worktree) if part]
        return "\n".join(parts)

    def history(self, limit: int = 20, include_autosave: bool = False) -> list[Entry]:
        if not self.has_commits():
            return []
        limit = max(1, min(limit, 500))
        # 多取一些再过滤，否则过滤掉自动快照后可能不够 limit 条。
        raw = self.text(
            "log", f"-n{limit * 3 if not include_autosave else limit}",
            "--pretty=format:%H%x1f%ad%x1f%s%x1f%(trailers:key=Radar-Version,valueonly,separator=)%x1e",
            "--date=format:%Y-%m-%d %H:%M",
        )
        entries: list[Entry] = []
        for chunk in raw.split("\x1e"):
            chunk = chunk.strip("\n")
            if not chunk:
                continue
            fields = chunk.split("\x1f")
            if len(fields) < 3:
                continue
            sha, when, subject = fields[0], fields[1], fields[2]
            version = fields[3].strip() if len(fields) > 3 else ""
            autosave = subject.startswith(AUTOSAVE_PREFIX)
            if autosave and not include_autosave:
                continue
            entries.append(Entry(sha, when, subject, version, autosave))
        return entries[:limit]

    def head_sha(self) -> str:
        return self.text("rev-parse", "HEAD").strip()

    def resolve(self, revision: str) -> str:
        """把用户在列表里看到的序号，或 sha / tag，解析成完整 sha。"""
        revision = (revision or "").strip()
        if not revision:
            raise VcsError("没有指定要退回的版本。先运行 `radar.py list` 看编号。")
        if revision.isdigit():
            index = int(revision)
            if index < 1:
                raise VcsError("编号从 1 开始，1 是最近一次保存。")
            target = f"HEAD~{index - 1}"
            if self.git("rev-parse", "--verify", "-q", target, check=False).returncode != 0:
                raise VcsError(f"没有第 {index} 个版本。运行 `radar.py list` 看看有哪几个。")
            return self.text("rev-parse", target).strip()
        result = self.git("rev-parse", "--verify", "-q", f"{revision}^{{commit}}", check=False)
        if result.returncode != 0:
            raise VcsError(f"找不到版本 {revision}：既不是编号，也不是 sha / tag。")
        return _decode(result.stdout).strip()

    # -- 写操作 -------------------------------------------------------------

    def save(self, message: str) -> Entry | None:
        """提交当前改动。没有任何改动就返回 None。"""
        if not self.exists():
            self.ensure()
        self.git("add", "-A")
        staged = self.git("diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return None
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        trailer = f"Radar-Version: {project_version()}\nRadar-Saved: {stamp}"
        self.git("commit", "-m", message.strip() or "更新", "-m", trailer, "--no-verify")
        sha = self.head_sha()
        return Entry(sha, time.strftime("%Y-%m-%d %H:%M"), message.strip() or "更新",
                     project_version(), False)

    def _pin_current(self, reason: str) -> str | None:
        """回滚前把当前状态固定成一个带 tag 的提交，保证随时能回来。"""
        if not self.has_commits():
            return None
        pending = self.pending()
        head = self.head_sha()
        # 同一秒内连着回滚两次会撞名，加毫秒后缀。
        tag = f"{AUTOSAVE_TAG}{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"
        if pending:
            self.git("add", "-A")
            stamp = time.strftime("%Y-%m-%d %H:%M")
            self.git("commit", "-m", f"{AUTOSAVE_PREFIX}（{reason} {stamp}）",
                     "-m", f"Radar-Version: {project_version()}\nRadar-Saved: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
                     "--no-verify")
            head = self.head_sha()
        self.git("tag", "-f", tag, head)
        return tag

    def rollback(self, target: str, reason: str = "回滚") -> tuple[str | None, str]:
        """把工作区恢复到 target，返回 (备份 tag, 目标 sha)。

        目标 sha 必须在留底之前解析完 —— 否则 pin 产生的新提交会把 HEAD 挪走，
        "丢弃未保存改动"这种目标等于当前 HEAD 的操作就会自己指向自己。
        """
        if not self.exists() or not self.has_commits():
            raise VcsError("还没有任何保存过的版本，没得回滚。")
        sha = self.resolve(target)
        tag = self._pin_current(reason)
        self.git("reset", "--hard", sha)
        return tag, sha

    def restore_config(self) -> bool:
        """只把 config/settings.json 退回上一次保存的状态。"""
        relative = "config/settings.json"
        if self.git("cat-file", "-e", f"HEAD:{relative}", check=False).returncode != 0:
            raise VcsError("上一次保存里没有配置文件，无法单独恢复。")
        self.git("checkout", "HEAD", "--", relative)
        return True

    def tag_version(self, name: str) -> None:
        self.git("tag", "-f", name, "HEAD")

    # -- 检查 ---------------------------------------------------------------

    def compile_check(self) -> tuple[bool, str]:
        code = (
            "import compileall,sys;"
            "ok=compileall.compile_dir('app',quiet=1) and compileall.compile_dir('tools',quiet=1) "
            "and compileall.compile_dir('scripts',quiet=1) and compileall.compile_dir('tests',quiet=1);"
            "sys.exit(0 if ok else 1)"
        )
        result = subprocess.run([str(python_exe()), "-c", code], cwd=str(self.root),
                                capture_output=True, timeout=180)
        return result.returncode == 0, _decode(result.stdout + result.stderr)[-2000:]

    def run_tests(self, pattern: str = "tests.test_core") -> tuple[bool, str]:
        result = subprocess.run(
            [str(python_exe()), "-m", "unittest", pattern],
            cwd=str(self.root), capture_output=True, timeout=900,
        )
        output = _decode(result.stderr) + _decode(result.stdout)
        return result.returncode == 0, output[-4000:]

    # -- 展示 ---------------------------------------------------------------

    def summary(self) -> str:
        """给 status 用的一段人话。"""
        lines = [f"项目版本：{project_version()}"]
        if not self.exists():
            lines.append("还没有版本库（运行 `radar.py save \"说明\"` 会自动建）")
            return "\n".join(lines)
        entries = self.history(1)
        if entries:
            last = entries[0]
            lines.append(f"最后一次保存：{last.when}  {last.subject}  ({last.short})")
        else:
            lines.append("还没有保存过任何版本")
        pending = self.pending()
        if pending:
            lines.append(f"未保存的改动：{len(pending)} 个文件")
            for status, path in pending[:12]:
                lines.append(f"    {_explain_status(status)}  {path}")
            if len(pending) > 12:
                lines.append(f"    …还有 {len(pending) - 12} 个")
            lines.append("  → 确认没问题后运行 `radar.py save \"这次改了什么\"`")
        else:
            lines.append("工作区干净，和上一次保存完全一致")
        return "\n".join(lines)


def _explain_status(code: str) -> str:
    return {"M": "改", "A": "增", "D": "删", "R": "移", "??": "新", "MM": "改", "AM": "增"}.get(code, code)


def long_help() -> str:
    return """版本控制（改坏了随时退回去）

  radar.py save "说明"      存一个版本。说明写人话，比如"修好图注截断"
  radar.py list             看最近 20 个版本，带编号
  radar.py status           看现在有哪些改动还没存
  radar.py undo             退回上一个版本（当前改动自动留底）
  radar.py back 3           退回到列表里第 3 个版本
  radar.py config-restore   只把设置文件退回上次保存的状态
  radar.py check            语法检查 + 跑单元测试
  radar.py tag v1.0         给当前版本起个名字
"""
