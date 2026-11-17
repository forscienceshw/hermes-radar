"""把一个版本发出去：bump 版本 → 打 tag → 建 Release → 传安装包 → 回写 version.json。

    python radar.py release "修好了封面防盗链，顺手加了自动更新"
    python radar.py release "..." --bump minor      # 1.0.0 → 1.1.0
    python radar.py release "..." --dry-run         # 只检查，不发布
    python radar.py release "..." --push-proxy http://127.0.0.1:7897

为什么全程走 API 而不是 git push：这台机器的网络里 github.com 十次有九次连不上，
但 api.github.com 和 uploads.github.com 一直通。所以 tag、Release、安装包上传、
version.json 全部走 gh api，一条命令都不碰 github.com 的网页域名。

代码本身（master 分支的提交）还是要 git push 的，那一步失败不会毁掉已经建好的
Release —— 脚本会告诉你补哪一条命令。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.vcs import Repo, project_version  # noqa: E402

OWNER = "forscienceshw"
REPO = "hermes-radar"
ASSET = "RadarSetup.exe"
VERSION_FILE = ROOT / "VERSION"
MANIFEST = ROOT / "version.json"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def say(text: str = "") -> None:
    print(text, flush=True)


def die(text: str) -> None:
    say(f"\n失败：{text}")
    raise SystemExit(1)


def run(command: list[str], cwd: Path | None = None, capture: bool = False,
        check: bool = True) -> subprocess.CompletedProcess:
    say("  $ " + " ".join(command))
    result = subprocess.run(command, cwd=str(cwd or ROOT), text=True, encoding="utf-8",
                            errors="replace",
                            capture_output=capture)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-1200:]
        die(f"命令返回 {result.returncode}\n{detail}")
    return result


def gh_api(path: str, method: str = "GET", payload: dict | None = None,
           allow_404: bool = False) -> tuple[int, dict | list | None]:
    """走 gh api（认证和 API 版本它自己管）。payload 用临时文件传，避免引号地狱。"""
    command = ["gh", "api", "--method", method, path]
    temp: Path | None = None
    if payload is not None:
        temp = ROOT / ".tmp" / "gh-payload.json"
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        command += ["--input", str(temp)]
    say("  $ gh api " + " ".join(command[3:]))
    result = subprocess.run(command, cwd=str(ROOT), text=True, encoding="utf-8",
                            errors="replace", capture_output=True)
    if temp:
        temp.unlink(missing_ok=True)
    if result.returncode != 0:
        if allow_404 and ("404" in result.stderr or "Not Found" in result.stderr):
            return 404, None
        die(f"GitHub API 失败（{method} {path}）\n{(result.stderr or '').strip()[-1200:]}")
    text = (result.stdout or "").strip()
    if not text:
        return result.returncode, None
    try:
        return 0, json.loads(text)
    except ValueError:
        return 0, None


def bump(version: str, part: str) -> str:
    pieces = [int(item) for item in (version.split(".") + ["0", "0"])[:3]]
    index = {"major": 0, "minor": 1, "patch": 2}[part]
    pieces[index] += 1
    for tail in range(index + 1, 3):
        pieces[tail] = 0
    return ".".join(str(item) for item in pieces)


def tag_exists(name: str) -> bool:
    status, _ = gh_api(f"repos/{OWNER}/{REPO}/git/ref/tags/{name}", allow_404=True)
    return status == 0


# --------------------------------------------------------------------------- #
# 各步骤
# --------------------------------------------------------------------------- #
def preflight(branch_ok: bool, run_tests: bool = True) -> str:
    say("① 检查工作区")
    repo = Repo(ROOT)
    if not repo.has_commits():
        die("这个目录不是 git 仓库，没法发布。")
    branch = repo.text("rev-parse", "--abbrev-ref", "HEAD").strip()
    say(f"  分支 {branch} / 版本 {project_version()}")
    if branch_ok and branch != "main":
        die(f"发布应该从 main 走，现在是 {branch}。先在开发目录跑 git merge --ff-only，"
            f"或者确认这就是想发的内容（加 --any-branch 跳过这个检查）。")

    pending = repo.pending()
    if pending:
        die("还有没提交的改动，先 radar.py save \"说明\"：\n  " +
            "\n  ".join(f"{state} {path}" for state, path in pending[:10]))

    if not run_tests:
        say("② 跳过测试（--skip-tests）")
        return branch

    say("② 跑测试")
    # 用安装目录自己的虚拟环境跑：PATH 里的 python 往往没装 httpx/fastapi，
    # 一跑就是一片 ImportError，看着像代码坏了。
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    python = str(venv_python) if venv_python.is_file() else sys.executable
    result = subprocess.run([python, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                            cwd=str(ROOT), text=True, encoding="utf-8", errors="replace",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines = (result.stdout or "").strip().splitlines()
    if result.returncode != 0:
        # 别只甩最后几行：失败用例的名字才是能动手的东西。
        bad = [line for line in lines if line.startswith(("ERROR:", "FAIL:"))]
        if bad:
            say("  这些没过：")
            say("  " + "\n  ".join(bad[:12]))
        say("  " + "\n  ".join(lines[-6:]))
        die("测试没过，不发布。查清楚再发，或者确认是环境抖动后加 --skip-tests。")
    say("  " + "\n  ".join(lines[-4:]))
    return branch


def set_version(version: str) -> None:
    VERSION_FILE.write_text(version + "\n", encoding="utf-8")


def commit_and_tag(version: str, message: str, push: bool, proxy: str) -> str:
    tag = f"v{version}"
    if commit_worktree():
        say(f"  已提交版本号 {version}")
    run(["git", "tag", tag])
    sha = run(["git", "rev-parse", "HEAD"], capture=True).stdout.strip()
    say(f"  本地 tag {tag} → {sha[:8]}")

    if push:
        command = ["git"]
        if proxy:
            command += ["-c", f"http.proxy={proxy}"]
        command += ["push", "origin", "main"]
        result = run(command + ["--dry-run"], capture=True, check=False)
        if result.returncode == 0:
            run(["git", *(["-c", f"http.proxy={proxy}"] if proxy else []), "push", "origin", "main"])
            run(["git", *(["-c", f"http.proxy={proxy}"] if proxy else []), "push", "origin", tag], check=False)
        else:
            say("  代码推送没成功（多半是 github.com 连不上），Release 照常发。"
                f"\n  网络好了补一句：git -c http.proxy={proxy or 'http://127.0.0.1:7897'} push origin main --tags")
    return sha


def commit_worktree() -> bool:
    status = run(["git", "status", "--porcelain"], capture=True).stdout.strip()
    if not status:
        return False
    run(["git", "add", "-A"])
    run(["git", "commit", "-m", "release: 版本号更新"])
    return True


def build() -> Path:
    say("③ 打安装包（PyInstaller，约两分钟）")
    builder = Path("E:/HermesRadarBuild/.venv/Scripts/python.exe")
    python = str(builder) if builder.is_file() else sys.executable
    # 必须两个一起打：--setup 只是把已有的 Radar.exe 塞进安装包，dist/exe 被清空过就没有它。
    result = subprocess.run([python, "tools/build_exe.py"], cwd=str(ROOT),
                            text=True, encoding="utf-8", errors="replace", capture_output=True)
    if result.returncode != 0:
        say((result.stdout or "")[-3000:])
        die("PyInstaller 失败，往上翻日志。")
    exe = ROOT / "dist" / "exe" / ASSET
    if not exe.is_file():
        die(f"没找到产物 {exe}")
    say(f"  {exe}  {exe.stat().st_size / 1024 / 1024:.1f} MB")
    return exe


def publish(version: str, title: str, notes: str, asset: Path) -> int:
    tag = f"v{version}"
    say(f"④ 建 Release {tag}")
    status, existing = gh_api(f"repos/{OWNER}/{REPO}/releases/tags/{tag}", allow_404=True)
    if status == 0 and isinstance(existing, dict):
        release_id = int(existing.get("id") or 0)
        say(f"  Release 已存在（id={release_id}），改成覆盖安装包")
        gh_api(f"repos/{OWNER}/{REPO}/releases/assets/{_asset_id(existing)}", "DELETE", allow_404=True)
        upload(version, release_id, asset)
        return release_id

    sha = run(["git", "rev-parse", "HEAD"], capture=True).stdout.strip()
    if not tag_exists(tag):
        gh_api(f"repos/{OWNER}/{REPO}/git/refs", "POST",
               {"ref": f"refs/tags/{tag}", "sha": sha})

    status, payload = gh_api(f"repos/{OWNER}/{REPO}/releases", "POST", {
        "tag_name": tag,
        "target_commitish": "main",
        "name": f"科研雷达 {tag}",
        "body": notes,
        "draft": False,
        "prerelease": False,
    })
    release_id = int((payload or {}).get("id") or 0)
    say(f"  Release id={release_id}")
    upload(version, release_id, asset)
    return release_id


def _asset_id(release: dict) -> int:
    for item in release.get("assets") or []:
        if str(item.get("name", "")).lower() == ASSET.lower():
            return int(item.get("id") or 0)
    return 0


def upload(version: str, release_id: int, asset: Path) -> None:
    say(f"⑤ 上传安装包（{asset.stat().st_size / 1024 / 1024:.1f} MB，可能要几分钟）")
    result = subprocess.run(
        ["gh", "release", "upload", f"v{version}", str(asset), "--clobber", "--repo", f"{OWNER}/{REPO}"],
        cwd=str(ROOT), text=True, encoding="utf-8", errors="replace", capture_output=True)
    if result.returncode != 0:
        say((result.stderr or "")[-1500:])
        die("安装包没传上去。Release 已经建好了，网络恢复后重跑这一句即可：\n"
            f"  gh release upload v{version} \"{asset}\" --clobber --repo {OWNER}/{REPO}")
    say("  上传完成")


def write_manifest(version: str, notes: str, asset: Path, push: bool, proxy: str) -> Path:
    say("⑥ 回写 version.json（各机器的客户端靠它发现新版本）")
    sha256 = hashlib.sha256(asset.read_bytes()).hexdigest()
    payload = {
        "version": version,
        "released_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "notes": notes.splitlines()[0][:200] if notes else "",
        "notes_url": f"https://github.com/{OWNER}/{REPO}/releases/tag/v{version}",
        "min_version": "",
        "asset": {
            "name": ASSET,
            "url": f"https://github.com/{OWNER}/{REPO}/releases/download/v{version}/{ASSET}",
            "size": asset.stat().st_size,
            "sha256": sha256,
        },
    }
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 只走 git push，不用 API 写文件。
    # 用 API 写会在远端凭空生成一个 commit，本地这边也有一个改了同样内容的 commit，
    # 下次 push 必然被拒 —— 今天已经踩过一次。
    run(["git", "add", "version.json"])
    run(["git", "commit", "-m", f"version.json: {version}"], check=False)
    if push:
        command = ["git", *(["-c", f"http.proxy={proxy}"] if proxy else []), "push", "origin", "main"]
        result = run(command, capture=True, check=False)
        if result.returncode != 0:
            say((result.stderr or "")[-600:])
            die("version.json 没推上去，各机器读不到新版本。网络恢复后补一句：\n"
                f"  git -c http.proxy={proxy or 'http://127.0.0.1:7897'} push origin main")
    say(f"  sha256 {sha256[:16]}…  {asset.stat().st_size / 1024 / 1024:.1f} MB")
    return MANIFEST


# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="发布一个新版本到 GitHub")
    parser.add_argument("notes", nargs="?", default="", help="这一版改了什么（一句话）")
    parser.add_argument("--bump", choices=("patch", "minor", "major"), default="patch",
                        help="版本号怎么涨，默认 patch")
    parser.add_argument("--version", default="", help="指定版本号，跳过自动 bump")
    parser.add_argument("--dry-run", action="store_true", help="只检查不发布")
    parser.add_argument("--skip-build", action="store_true", help="复用 dist/exe 里的旧产物")
    parser.add_argument("--skip-tests", action="store_true", help="跳过测试（只在确认是环境抖动时用）")
    parser.add_argument("--no-push", action="store_true", help="不推 git（只发 Release）")
    parser.add_argument("--any-branch", action="store_true", help="允许从非 main 分支发布")
    parser.add_argument("--push-proxy", default="", help="git push 用的代理")
    args = parser.parse_args(argv)

    say("=" * 60)
    say("发布 Hermes 科研雷达")
    say("=" * 60)
    preflight(branch_ok=not args.any_branch, run_tests=not args.skip_tests)

    current = project_version()
    version = args.version or bump(current, args.bump)
    say(f"  版本 {current} → {version}")
    if tag_exists(f"v{version}"):
        die(f"远端已经有 v{version} 这个 tag 了。换一个 --version，或者先删掉那个 Release。")

    notes = (args.notes or "").strip() or f"日常更新（{time.strftime('%Y-%m-%d')}）"
    if args.dry_run:
        say("\n干跑模式：检查都过了，没动任何东西。")
        return 0

    set_version(version)
    commit_and_tag(version, notes, push=not args.no_push, proxy=args.push_proxy)
    asset = (ROOT / "dist" / "exe" / ASSET) if args.skip_build else build()
    if not asset.is_file():
        die("没有安装包产物，去掉 --skip-build 再跑。")
    publish(version, notes, notes, asset)
    write_manifest(version, notes, asset, push=not args.no_push, proxy=args.push_proxy)

    say("\n" + "=" * 60)
    say(f"发完了：v{version}")
    say(f"  Release  https://github.com/{OWNER}/{REPO}/releases/tag/v{version}")
    say(f"  其它机器下次启动会在后台看到更新提示。")
    say("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
