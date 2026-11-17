"""看住微信的存储位置，别让它在 C 盘堆出几十个 G。

微信分两代，挪数据的机制完全不同，两条都得管：

* **3.x**：聊天文件默认在 `%USERPROFILE%\\Documents\\WeChat Files`。当年是用一条目录
  软链接把它指到别的盘的（本机指向 `E:\\电脑管家迁移文件\\微信聊天记录搬家\\WeChat Files`）。
  升级微信不会动这条链接，但重装/搬家工具可能会把它换回真目录。
* **4.x**：存储根目录写在 `%APPDATA%\\Tencent\\xwechat\\config\\<hash>.ini` 里，一行绝对
  路径（本机是 `E:\\电脑管家迁移文件`）。这个文件在升级、重装、换账号时都可能被重置，
  一旦没了，微信就回到 `Documents\\xwechat_files`，从此往 C 盘里灌。

所以这里的策略是"**双保险**"：既看住 ini，又在 `Documents\\xwechat_files` 上补一条
同目标的目录软链接。这样即使 ini 被重置，微信落到 Documents 下的数据也还是写在别的盘，
而且路径结构与 ini 指向时完全一致（都是 `<root>\\xwechat_files\\<wxid>`），不会出现两份。
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

XWECHAT_CONFIG = Path(os.environ.get("APPDATA", "")) / "Tencent" / "xwechat" / "config"
XWECHAT_DATA = Path(os.environ.get("APPDATA", "")) / "Tencent" / "xwechat"
WECHAT3_DATA = Path(os.environ.get("APPDATA", "")) / "Tencent" / "WeChat"
DOCUMENTS = Path.home() / "Documents"
WECHAT3_LINK = DOCUMENTS / "WeChat Files"
XWECHAT_LINK = DOCUMENTS / "xwechat_files"

# 这些目录只是安装包和日志，删了微信会自己重建，属于可以放心回收的部分。
CLEANABLE = (
    ("4.x 安装包缓存", XWECHAT_DATA / "update", 0),
    ("4.x 日志", XWECHAT_DATA / "log", 7),
    ("3.x 日志", WECHAT3_DATA / "log", 7),
)


def _on_c(path: Path | str) -> bool:
    try:
        return str(path).replace("/", "\\").upper().startswith("C:")
    except Exception:                                   # noqa: BLE001
        return False


def _size(path: Path) -> int:
    """目录大小。只做粗略统计，遇到没权限的项就跳过，绝不为了让数字好看而抛异常。"""
    if not path.exists():
        return 0
    total = 0
    for base, dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                continue
        # 别钻进软链接，不然会把整个 E 盘算进来
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
    return total


def _read_ini(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _four_x_root() -> tuple[Path | None, Path | None]:
    """4.x 的存储根目录从哪来：先看 ini，再退回实际扫描到的库目录。"""
    if XWECHAT_CONFIG.is_dir():
        for ini in XWECHAT_CONFIG.glob("*.ini"):
            value = _read_ini(ini)
            if value and re.match(r"^[A-Za-z]:\\", value):
                return Path(value), ini
    try:
        from . import wechat4_live
        for storage in wechat4_live.db_storages():
            # <root>\xwechat_files\<wxid>\db_storage
            parts = Path(storage).parts
            if "xwechat_files" in parts:
                index = parts.index("xwechat_files")
                return Path(*parts[:index]), None
    except Exception:                                   # noqa: BLE001
        pass
    return None, None


def _link_target(path: Path) -> str:
    """软链接/junction 的真实目标；普通目录返回空串。

    不能用 `Path.is_symlink()` 判断：Windows 的 junction（`mklink /J`，电脑管家
    那类搬家工具最爱用）在 Python 眼里 `islink()` 是 False，但 `readlink()`
    读得到。只认 islink 会把一条好好的 junction 判成"普通目录"。
    """
    try:
        return os.readlink(path)
    except OSError:
        return ""


def snapshot() -> dict[str, Any]:
    """一次性给出"微信到底把东西放哪、C 盘被占了多少"。"""
    four_root, ini = _four_x_root()
    three_target = _link_target(WECHAT3_LINK)
    four_target = _link_target(XWECHAT_LINK)

    sizes = {
        "xwechat": _size(XWECHAT_DATA),
        "wechat3": _size(WECHAT3_DATA),
        "local_tencent": _size(Path(os.environ.get("LOCALAPPDATA", "")) / "Tencent"),
    }
    free_c = 0
    try:
        free_c = shutil.disk_usage("C:\\").free if os.name == "nt" else 0
    except OSError:
        pass

    warnings: list[str] = []
    if four_root and _on_c(four_root):
        warnings.append(f"微信 4.x 的存储根目录还在 C 盘（{four_root}），聊天文件会继续堆在系统盘")
    if not XWECHAT_LINK.exists() and four_root and not _on_c(four_root):
        warnings.append("Documents\\xwechat_files 还没有指向别的盘；万一微信配置被重置，数据会落到 C 盘")
    if WECHAT3_LINK.exists() and not three_target:
        warnings.append("Documents\\WeChat Files 已经不是软链接了，旧版微信的数据会重新写进 C 盘")
    if free_c and free_c < 20 * 1024 ** 3:
        warnings.append(f"C 盘只剩 {free_c / 1024 ** 3:.1f} GB")

    return {
        "ok": True,
        "four_x": {
            "root": str(four_root) if four_root else "",
            "ini": str(ini) if ini else "",
            "on_c": _on_c(four_root) if four_root else False,
            "link": str(XWECHAT_LINK),
            "link_target": four_target,
        },
        "three_x": {
            "link": str(WECHAT3_LINK),
            "link_target": three_target,
            "on_c": bool(WECHAT3_LINK.exists() and not three_target),
        },
        "sizes": sizes,
        "cleanable": sum(_size(path) for _, path, _ in CLEANABLE if path.exists()),
        "free_c": free_c,
        "warnings": warnings,
    }


def ensure() -> dict[str, Any]:
    """把两条兜底链接补齐。幂等：已经对了就什么都不做。"""
    actions: list[str] = []
    errors: list[str] = []
    four_root, ini = _four_x_root()
    if four_root and not _on_c(four_root):
        # `Documents\xwechat_files` → `<root>\xwechat_files`，与 ini 生效时的路径结构一致
        want = Path(four_root) / "xwechat_files"
        if not XWECHAT_LINK.exists():
            try:
                want.mkdir(parents=True, exist_ok=True)
                os.symlink(str(want), str(XWECHAT_LINK), target_is_directory=True)
                actions.append(f"已建目录软链接 {XWECHAT_LINK} → {want}")
            except OSError as exc:
                errors.append(f"建 {XWECHAT_LINK} 失败：{exc}")
        elif _link_target(XWECHAT_LINK) and Path(_link_target(XWECHAT_LINK)).resolve() != want.resolve():
            errors.append(f"{XWECHAT_LINK} 已指向别处（{_link_target(XWECHAT_LINK)}），没有自动改")
    if ini and four_root and _on_c(four_root):
        errors.append(f"{ini} 里写的是 C 盘路径；请先在微信设置里把存储位置改到别的盘，再回来复查")
    return {"ok": not errors, "actions": actions, "errors": errors, "snapshot": snapshot()}


def clean(dry_run: bool = True) -> dict[str, Any]:
    """回收安装包缓存和旧日志。只碰 CLEANABLE 里列出的目录。"""
    removed: list[dict[str, Any]] = []
    cutoff = time.time() - 7 * 86400
    for label, path, keep_days in CLEANABLE:
        if not path.exists():
            continue
        if keep_days <= 0:
            targets = list(path.iterdir()) if path.is_dir() else [path]
        else:
            targets = [item for item in path.rglob("*")
                       if item.is_file() and item.stat().st_mtime < cutoff]
        # 先算体积再删：dry-run 时什么都没动，用"删前减删后"会永远得 0。
        freed = 0
        for item in targets:
            try:
                freed += _size(item) if item.is_dir() else item.stat().st_size
            except OSError:
                continue
        if not dry_run:
            from . import purge
            purge.remove(*targets)
        removed.append({"label": label, "path": str(path), "freed": freed, "items": len(targets)})
    return {"dry_run": dry_run, "freed": sum(item["freed"] for item in removed), "items": removed}
