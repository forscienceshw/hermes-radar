"""扫一遍代码，找出会让 Windows 弹一下黑框的子进程调用。

背景：图形宿主（``pythonw.exe``、PyInstaller ``--windowed`` 的 exe）自己没有控制台。
从这种进程里启动控制台程序（``tasklist`` / ``python -c`` / ``pip`` / ``node`` …），
如果不带 ``CREATE_NO_WINDOW``，Windows 会**给子进程新建一个控制台窗口**——用户看到
黑框一闪，以为程序坏了。这个脚本把这类调用全部找出来，让 CI 或 ``radar.py check``
在合并前就拦住。

判定规则：

* 认出 ``subprocess.Popen/run/call/check_call/check_output``、``asyncio.create_subprocess_*``、
  ``os.system/os.popen``；
* 调用里必须有 ``creationflags=`` 或 ``startupinfo=``，或者用 ``**kwargs`` 展开
  （例如 ``**_hidden()``）；
* 想放行，在调用所在行（或上一行）写 ``# console-ok: 原因``。放行的理由只能是
  "这条命令本来就要给人看输出/输入"，不能是"懒得改"。

用法：

    python tools/console_audit.py               # 按 .console-audit.json 的范围扫
    python tools/console_audit.py --all         # 扫全仓库
    python tools/console_audit.py --paths app installer supervisor.pyw
    python tools/console_audit.py --json        # 给编辑器/CI 用

退出码：0 = 没有会弹窗的调用；1 = 有；2 = 参数或配置有问题。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = ".console-audit.json"
MARKER = "# console-ok"

SUBPROCESS_FUNCS = {"Popen", "run", "call", "check_call", "check_output", "getoutput"}
ASYNCIO_FUNCS = {"create_subprocess_exec", "create_subprocess_shell"}
OS_FUNCS = {"system", "popen"}

DEFAULT_SKIP = {
    "__pycache__", ".git", ".venv", "venv", "env", "node_modules", "dist", "build",
    ".build", ".tmp", "site-packages", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}


@dataclass(frozen=True)
class Spawn:
    path: Path
    line: int
    end_line: int
    kind: str
    snippet: str
    allowed: bool
    reason: str


def _spawn_kind(node: ast.AST, imported: set[str]) -> str | None:
    """认出这是一个"起子进程"的调用；不是就返回 None。"""
    if isinstance(node, ast.Name) and node.id in imported:
        return f"subprocess.{node.id}"
    if not isinstance(node, ast.Attribute):
        return None
    owner = node.value
    if isinstance(owner, ast.Name):
        if owner.id == "subprocess" and node.attr in SUBPROCESS_FUNCS:
            return f"subprocess.{node.attr}"
        if owner.id == "asyncio" and node.attr in ASYNCIO_FUNCS:
            return f"asyncio.{node.attr}"
        if owner.id == "os" and node.attr in OS_FUNCS:
            return f"os.{node.attr}"
    return None


def _hidden_keyword(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg is None:                 # **kwargs：交给调用方保证（如 **_hidden()）
            return True
        if keyword.arg in {"creationflags", "startupinfo"}:
            return True
    return False


def _imported_names(tree: ast.Module) -> set[str]:
    """`from subprocess import Popen as spawn` 这种别名也要认。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SUBPROCESS_FUNCS:
                    names.add(alias.asname or alias.name)
    return names


def scan_file(path: Path, root: Path) -> list[Spawn]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return []
    lines = source.splitlines()
    imported = _imported_names(tree)
    found: list[Spawn] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _spawn_kind(node.func, imported)
        if kind is None:
            continue
        end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
        window = lines[max(0, node.lineno - 2):end_line]
        marker = next((text for text in window if MARKER in text), "")
        snippet = (lines[node.lineno - 1].strip() if node.lineno - 1 < len(lines) else "")[:100]
        found.append(Spawn(
            path=path.relative_to(root),
            line=node.lineno,
            end_line=end_line,
            kind=kind,
            snippet=snippet,
            allowed=bool(marker) or _hidden_keyword(node),
            reason=marker.split(MARKER, 1)[1].strip(" :：") if marker else "",
        ))
    return found


def _iter_files(root: Path, targets: list[str], skip: set[str]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        base = (root / target).resolve()
        if base.is_file():
            if base.suffix in {".py", ".pyw"}:
                files.append(base)
            continue
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix not in {".py", ".pyw"}:
                continue
            if any(part in skip for part in path.relative_to(root).parts):
                continue
            files.append(path)
    return sorted(set(files))


def _load_config(root: Path) -> dict:
    path = root / CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{CONFIG_NAME} 读不了：{exc}")


def audit(root: Path, targets: list[str] | None = None, skip: set[str] | None = None) -> list[Spawn]:
    config = _load_config(root)
    chosen = list(targets or config.get("paths") or ["."])
    ignored = set(DEFAULT_SKIP) | set(config.get("skip") or []) | set(skip or ())
    spawns: list[Spawn] = []
    for path in _iter_files(root, chosen, ignored):
        spawns.extend(scan_file(path, root))
    return spawns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="找出会让 Windows 弹黑框的子进程调用")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                        help="仓库根目录（默认是本脚本上一层）")
    parser.add_argument("--paths", nargs="+", help="只扫这些路径（文件或目录）")
    parser.add_argument("--all", action="store_true", help="忽略配置里的范围，扫全仓库")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="只在有违规时输出")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"根目录不存在：{root}", file=sys.stderr)
        return 2
    targets = ["."] if args.all else args.paths
    try:
        spawns = audit(root, targets)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    bad = [item for item in spawns if not item.allowed]
    if args.json:
        print(json.dumps({
            "scanned": len({item.path for item in spawns}),
            "spawns": len(spawns),
            "violations": [
                {"file": str(item.path), "line": item.line, "kind": item.kind,
                 "snippet": item.snippet}
                for item in bad
            ],
        }, ensure_ascii=False, indent=2))
    elif bad:
        for item in bad:
            print(f"✗ {item.path}:{item.line}  {item.kind}")
            print(f"    {item.snippet}")
            print("    → 加 creationflags=CREATE_NO_WINDOW，或用 app/winproc.py 的 hidden_kwargs()")
        print(f"\n{len(bad)} 处会弹窗，共 {len(spawns)} 处子进程调用。")
    elif not args.quiet:
        print(f"✓ 没有会弹窗的调用（{len(spawns)} 处子进程调用，"
              f"{len({item.path for item in spawns})} 个文件）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
