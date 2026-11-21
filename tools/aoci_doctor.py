"""AOCI 体检与修复：确认「索引 + 可执行文件 + MCP 配置 + 跨目录绑定」四件事都在。

为什么需要它
------------
AOCI 二进制放在仓库外（`D:/program/WorkBuddyData/tools/aoci/`），索引在仓库里，
MCP 由 WorkBuddy 启动。三者任何一处断掉，表现都只是「AOCI 不存在 / 没有工具」，
不报错、不留日志。2026-11-21 的事故就是 shim 只按当前目录找 `aoci.txt`：
会话开在别的项目目录里，于是 MCP 返回 0 个工具，看起来像整个工具链消失了。

用法：
    python tools/aoci_doctor.py            # 只体检，不改任何文件
    python tools/aoci_doctor.py --fix      # 补 repos.json、重写 MCP 条目、复制 shim
    python tools/aoci_doctor.py --json     # 给脚本/自动化用

退出码 0 = 全部通过；1 = 有失败项。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GLOBAL = Path(os.environ.get("AOCI_HOME") or r"D:/program/WorkBuddyData/tools/aoci")
EXE_NAMES = ("aoci.exe", "aoci")
INDEX_FILES = ("aoci.txt", "aoci.code.txt", "aoci.meta.txt")


def _config_candidates() -> list[Path]:
    out: list[Path] = []
    for env in ("WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR"):
        value = os.environ.get(env)
        if value:
            out.append(Path(value) / "mcp.json")
    out.append(Path.home() / ".workbuddy" / "mcp.json")
    out.append(Path(r"D:/program/WorkBuddyData/workbuddy_data/mcp.json"))
    out.append(GLOBAL / "mcp.json")
    seen, unique = set(), []
    for item in out:
        key = str(item).lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _find_exe() -> Path | None:
    env = os.environ.get("AOCI_EXE")
    if env and Path(env).is_file():
        return Path(env)
    for name in EXE_NAMES:
        for base in (GLOBAL, REPO / "tools" / "aoci"):
            candidate = base / name
            if candidate.is_file():
                return candidate
    return None


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd or REPO), capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def check(config: Path | None = None) -> dict:
    result: dict = {"ok": True, "checks": [], "repo": str(REPO)}

    def add(name: str, passed: bool, detail: str, fix: str = "") -> None:
        result["checks"].append({"name": name, "ok": bool(passed), "detail": detail, "fix": fix})
        if not passed:
            result["ok"] = False

    exe = _find_exe()
    add("aoci 可执行文件", bool(exe), str(exe) if exe else "未找到 aoci.exe",
        "把 aoci.exe 放回 D:/program/WorkBuddyData/tools/aoci/，或设置 AOCI_EXE")
    if exe:
        version = ""
        try:
            for flag in ("--version", "version"):
                version = _run([str(exe), flag]).stdout.strip()
                if version:
                    break
            add("版本可读", bool(version), version or "version 输出为空",
                "重新解压 aoci_0.2.0-rc3_windows_amd64.zip")
        except Exception as exc:  # noqa: BLE001
            add("版本可读", False, f"执行失败：{exc}", "检查二进制是否被隔离/损坏")

    missing = [name for name in INDEX_FILES if not (REPO / name).is_file()]
    add("索引文件", not missing, "齐全" if not missing else f"缺 {', '.join(missing)}",
        "git checkout -- aoci.txt aoci.meta.txt aoci.code.txt")

    if exe and not missing:
        try:
            proc = _run([str(exe), "check"], timeout=180)
            text = (proc.stdout or "") + (proc.stderr or "")
            tail = [line for line in text.splitlines() if line.strip()][-6:]
            # check 对「索引漂移」返回 0，只把真正的执行错误当失败。
            crashed = proc.returncode != 0 and "panic" in text.lower()
            add("索引自检", not crashed, " | ".join(tail) if tail else "无输出",
                "运行 aoci maintain 处理漂移")
        except Exception as exc:  # noqa: BLE001
            add("索引自检", False, f"执行失败：{exc}", "")

    shim = GLOBAL / "aoci_mcp_shim.py"
    add("MCP shim", shim.is_file(), str(shim) if shim.is_file() else "shim 缺失",
        "python tools/aoci_doctor.py --fix（从仓库复制）")

    registry = GLOBAL / "repos.json"
    default = ""
    try:
        default = str((json.loads(registry.read_text("utf-8")) or {}).get("default") or "")
    except (OSError, ValueError):
        pass
    add("仓库注册表", bool(default and Path(default, "aoci.txt").is_file()),
        f"default={default or '未设置'}",
        "python tools/aoci_doctor.py --fix（写入当前仓库绝对路径）")

    if shim.is_file():
        try:
            with tempfile.TemporaryDirectory() as tmp:
                proc = _run([sys.executable, str(shim), "--resolve"], cwd=Path(tmp), timeout=60)
            info = json.loads(proc.stdout or "{}")
            bound = str(info.get("repo") or "")
            add("跨目录绑定", Path(bound).resolve() == REPO.resolve() if bound else False,
                f"{info.get('reason')} -> {bound or '未绑定'}",
                "检查 repos.json 的 default 是否指向本仓库")
        except Exception as exc:  # noqa: BLE001
            add("跨目录绑定", False, f"解析失败：{exc}", "")

    config = config or next((p for p in _config_candidates() if p.is_file()), None)
    entry = {}
    if config and config.is_file():
        try:
            entry = (json.loads(config.read_text("utf-8")).get("mcpServers") or {}).get("aoci") or {}
        except (OSError, ValueError):
            entry = {}
    args = entry.get("args") or []
    shim_ok = bool(args) and Path(str(args[-1])).is_file()
    add("MCP 配置", bool(entry) and shim_ok,
        f"{config} -> {entry.get('command', '')} {' '.join(map(str, args))}".strip() if entry else "没有 aoci 条目",
        "python tools/aoci_doctor.py --fix")
    return result


def fix(config: Path | None = None) -> dict:
    GLOBAL.mkdir(parents=True, exist_ok=True)
    source = REPO / "tools" / "aoci_mcp_shim.py"
    if source.is_file():
        shutil.copy2(source, GLOBAL / "aoci_mcp_shim.py")

    registry_path = GLOBAL / "repos.json"
    try:
        registry = json.loads(registry_path.read_text("utf-8"))
    except (OSError, ValueError):
        registry = {}
    repos = [str(REPO).replace("\\", "/")]
    for item in registry.get("repos") or []:
        if isinstance(item, str) and item.strip() and Path(item).resolve() != REPO.resolve():
            repos.append(item)
    registry = {"default": str(REPO).replace("\\", "/"), "repos": repos}
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    target = config or next((p for p in _config_candidates() if p.is_file()),
                            next(iter(_config_candidates())))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    servers = data.setdefault("mcpServers", {})
    # 用仓库外的稳定解释器：.venv 被重建/删除时 MCP 不该跟着坏。
    python = Path(r"D:/Python/python.exe")
    servers["aoci"] = {
        "command": str(python) if python.is_file() else sys.executable,
        "args": [str(GLOBAL / "aoci_mcp_shim.py")],
        "description": "AOCI 仓库认知层：跨会话/跨压缩保存整个代码库的 F/R/A/S 语义地图（项目内需先 aoci init）",
    }
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"config": str(target), "shim": str(GLOBAL / "aoci_mcp_shim.py"),
            "registry": str(registry_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="AOCI 体检与修复")
    parser.add_argument("--fix", action="store_true", help="修复 shim、注册表与 MCP 配置")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--config", default="", help="指定 mcp.json 路径")
    args = parser.parse_args()
    config = Path(args.config) if args.config else None

    fixed = fix(config) if args.fix else None
    report = check(config)
    if fixed:
        report["fixed"] = fixed
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["checks"]:
            mark = "PASS" if item["ok"] else "FAIL"
            print(f"[{mark}] {item['name']}: {item['detail']}")
            if not item["ok"] and item["fix"]:
                print(f"       -> {item['fix']}")
        if fixed:
            print(f"\n已修复：shim={fixed['shim']}")
            print(f"        registry={fixed['registry']}")
            print(f"        config={fixed['config']}")
        print("\n结论：" + ("全部通过" if report["ok"] else "有失败项，按上面的提示处理"))
        if not report["ok"]:
            print("注意：MCP 配置改动要重启 WorkBuddy 才会重新加载工具。")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
