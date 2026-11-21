"""AOCI MCP shim for WorkBuddy / CodeBuddy.

Why this exists
---------------
`aoci mcp` binds to the repository of the *current working directory* and exits
when there is no `aoci.txt`.  WorkBuddy launches MCP servers for every session,
including sessions whose cwd is a normal project folder, so a bare `aoci mcp`
entry silently becomes "AOCI 不存在" (zero tools) in those sessions.

This shim decouples the two:

1. cwd is inside an AOCI repository  -> bind that one (you may be working in a
   dev checkout, which should win over the default);
2. `AOCI_REPO` env var points at one -> bind it;
3. otherwise use the repository registry that sits next to this file
   (`repos.json`), whose `default` is the long-lived repo.

The registry is what makes AOCI survive sessions started from unrelated
folders.  Keep it current with `python tools/aoci_doctor.py --fix`.

Manual check:  python aoci_mcp_shim.py --resolve
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
AOCI_EXE = Path(os.environ.get("AOCI_EXE") or HERE / "aoci.exe")
REGISTRY = Path(os.environ.get("AOCI_REGISTRY") or HERE / "repos.json")
MARKER = "aoci.txt"
SERVER_INFO = {"name": "aoci-code", "version": "0.2.0-rc3+shim"}
PROTOCOL = "2025-06-18"


def _is_repo(path: Path | str | None) -> bool:
    return bool(path) and (Path(path) / MARKER).is_file()


def _registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _walk_up(start: Path) -> str | None:
    cur = start.resolve()
    while True:
        if (cur / MARKER).is_file():
            return str(cur)
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def find_repo(start: str | Path | None = None) -> tuple[str | None, str]:
    """Return (repo, reason).  Reason is what the doctor/diagnostics print."""
    env = str(os.environ.get("AOCI_REPO") or "").strip()
    if env and _is_repo(env):
        return str(Path(env).resolve()), "env:AOCI_REPO"
    here = _walk_up(Path(start or os.getcwd()))
    if here:
        return here, "cwd"

    data = _registry()
    candidates: list[str] = []
    default = str(data.get("default") or "").strip()
    if default:
        candidates.append(default)
    for item in data.get("repos") or []:
        if isinstance(item, str) and item.strip():
            candidates.append(item.strip())
    for candidate in candidates:
        if _is_repo(candidate):
            return str(Path(candidate).resolve()), "registry"
    return None, "unbound"


def proxy(repo: str) -> int:
    proc = subprocess.Popen(
        [str(AOCI_EXE), "mcp"],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def pump_in() -> None:
        try:
            while True:
                chunk = sys.stdin.buffer.read1(65536)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                proc.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    def pump_err() -> None:
        try:
            for line in proc.stderr:
                sys.stderr.buffer.write(line)
                sys.stderr.buffer.flush()
        except Exception:
            pass

    threading.Thread(target=pump_in, daemon=True).start()
    threading.Thread(target=pump_err, daemon=True).start()

    try:
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    except Exception:
        pass
    return proc.wait()


def reply(msg_id, result=None, error=None) -> None:
    body = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    sys.stdout.write(json.dumps(body) + "\n")
    sys.stdout.flush()


def idle_server() -> int:
    """Minimal MCP server with no tools: keeps the host quiet when unbound.

    It still answers initialize/tools/list, so the host does not show a crashed
    server; the reason goes to stderr and into the server instructions.
    """
    repo, reason = find_repo()
    note = (
        f"AOCI 未绑定仓库（{reason}）。执行 python tools/aoci_doctor.py --fix，"
        "或在 repos.json 的 default 写入仓库绝对路径。"
    )
    if repo is None:
        sys.stderr.write(note + "\n")
        sys.stderr.flush()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if msg_id is None:
            continue
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": note,
            })
        elif method == "tools/list":
            reply(msg_id, {"tools": []})
        elif method == "ping":
            reply(msg_id, {})
        else:
            reply(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})
    return 0


def main() -> int:
    if "--resolve" in sys.argv:
        repo, reason = find_repo()
        print(json.dumps({
            "repo": repo,
            "reason": reason,
            "exe": str(AOCI_EXE),
            "exe_exists": AOCI_EXE.is_file(),
            "registry": str(REGISTRY),
            "cwd": os.getcwd(),
        }, ensure_ascii=False))
        return 0 if repo and AOCI_EXE.is_file() else 1

    repo, _reason = find_repo()
    if repo and AOCI_EXE.is_file():
        return proxy(repo)
    return idle_server()


if __name__ == "__main__":
    sys.exit(main())
