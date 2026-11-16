import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from ..paths import BASE_DIR, DATA_DIR
from ..utils import humanize_error_text, now_iso

WORKER_PATH = BASE_DIR / "cloud" / "dist" / "llm-worker.mjs"
USAGE_LOG = DATA_DIR / "llm-usage.jsonl"
MAX_PAYLOAD_BYTES = 28_000_000



def _hidden_process_options() -> dict[str, Any]:
    """Keep the Node helper invisible: the parent runs under pythonw without a console."""
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def _payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def payload_summary(payload: dict[str, Any]) -> str:
    """Describe what was sent, so an opaque 400 can be traced to text or images."""
    images = chars = 0
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    images += 1
                elif part.get("type") == "text":
                    chars += len(part.get("text") or "")
        elif isinstance(content, str):
            chars += len(content)
    size = len(_payload_bytes(payload)) / 1024 / 1024
    return f"{chars} 字符文本、{images} 张图片、请求体 {size:.1f} MB"


async def _read_stderr(process: asyncio.subprocess.Process, state: dict[str, Any],
                       on_progress: Callable[[dict[str, Any]], None] | None) -> list[Any]:
    """Consume newline-delimited progress events and keep real error payloads."""
    errors: list[Any] = []
    assert process.stderr is not None
    while True:
        line = await process.stderr.readline()
        if not line:
            return errors
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            errors.append(text)
            continue
        if isinstance(data, dict) and data.get("event"):
            if data.get("event") == "start":
                state["seen"] = True
            if data.get("event") == "progress":
                state["last"] = time.monotonic()
                for key in ("contentChars", "reasoningChars", "chunks"):
                    if key in data:
                        state[key] = data[key]
            if data.get("event") == "done":
                state["last"] = time.monotonic()
            if on_progress:
                try:
                    on_progress(dict(state))
                except Exception:
                    pass
            continue
        errors.append(data)
    return errors


async def _watch_stall(process: asyncio.subprocess.Process, state: dict[str, Any], stall_seconds: float) -> None:
    while process.returncode is None:
        await asyncio.sleep(1.5)
        if not state.get("seen"):
            continue
        if time.monotonic() - state["last"] > stall_seconds:
            state["stalled"] = True
            try:
                process.kill()
            except ProcessLookupError:
                pass
            return


async def _run_worker(payload: dict[str, Any], timeout_seconds: float = 240,
                      on_progress: Callable[[dict[str, Any]], None] | None = None,
                      stall_seconds: float | None = None) -> dict[str, Any]:
    if not WORKER_PATH.is_file():
        raise RuntimeError("WorkBuddy 云调用组件尚未构建，请先运行 npm run build:cloud")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("未找到 Node.js 运行时")
    body = _payload_bytes(payload)
    if len(body) > MAX_PAYLOAD_BYTES:
        # Images dominate the payload; drop them rather than fail the whole reading.
        stripped = json.loads(body.decode("utf-8"))
        for message in stripped.get("messages") or []:
            if isinstance(message.get("content"), list):
                message["content"] = [part for part in message["content"] if part.get("type") == "text"]
        body = _payload_bytes(stripped)
        if len(body) > MAX_PAYLOAD_BYTES:
            raise RuntimeError(f"请求体 {len(body) / 1024 / 1024:.1f} MB 超过云接口 30 MB 上限，已跳过图片仍过大")
    env = os.environ.copy()
    env["NODE_NO_WARNINGS"] = "1"
    process = await asyncio.create_subprocess_exec(
        node, str(WORKER_PATH), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=str(BASE_DIR), env=env, **_hidden_process_options(),
    )
    state: dict[str, Any] = {"seen": False, "last": time.monotonic(), "contentChars": 0,
                             "reasoningChars": 0, "chunks": 0, "stalled": False,
                             "started": time.monotonic()}
    assert process.stdin is not None and process.stdout is not None
    stderr_task = asyncio.create_task(_read_stderr(process, state, on_progress))
    stall_task = asyncio.create_task(_watch_stall(process, state, stall_seconds or max(120.0, timeout_seconds * 0.4)))
    try:
        process.stdin.write(body)
        await process.stdin.drain()
        process.stdin.close()
        stdout = await asyncio.wait_for(process.stdout.read(), timeout=timeout_seconds)
        await asyncio.wait_for(process.wait(), timeout=max(10.0, timeout_seconds - (time.monotonic() - state["started"])))
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        elapsed = time.monotonic() - state["started"]
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        generated = int(state.get("contentChars") or 0)
        reasoning = int(state.get("reasoningChars") or 0)
        if generated or reasoning:
            raise TimeoutError(
                f"WorkBuddy 云模型 {int(elapsed)} 秒仍未读完（已推理 {reasoning} 字、已输出 {generated} 字）；"
                f"不是卡死，是这篇文章超出当前 {int(timeout_seconds)} 秒上限") from exc
        raise TimeoutError(
            f"WorkBuddy 云模型 {int(elapsed)} 秒内没有任何输出（判定为服务端卡住，而非文章太长）") from exc
    finally:
        stall_task.cancel()
        errors = await stderr_task
    if state.get("stalled"):
        raise TimeoutError(
            f"WorkBuddy 云模型连续 {int(stall_seconds or 0)} 秒没有吐出任何新内容，判定为卡住并已中止")
    if len(stdout) > 30_000_000:
        raise ValueError("云模型响应超过大小限制")
    if process.returncode != 0:
        message = code = retry_after = None
        error: Any = None
        for item in reversed(errors):
            if isinstance(item, dict):
                error = item
                break
        if error:
            message = humanize_error_text(error.get("message") or "", limit=600) or None
            code = error.get("code")
            retry_after = error.get("retryAfterMs")
        elif errors:
            message = humanize_error_text(str(errors[-1]))
        if not message:
            # An empty stderr used to surface as a bare "RuntimeError"; keep the exit code visible.
            snippet = humanize_error_text(stdout.decode("utf-8", errors="replace"), limit=300)
            message = f"云模型进程异常退出（退出码 {process.returncode}，未返回错误详情）"
            if snippet:
                message += f"；输出：{snippet}"
        if retry_after:
            await asyncio.sleep(min(60, retry_after / 1000))
        raise RuntimeError(f"{code + '：' if code else ''}{message}")
    try:
        result = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError:
        raise RuntimeError(f"云模型返回了无法解析的内容：{stdout[:200]!r}") from None
    if not result.get("ok"):
        raise RuntimeError(result.get("message") or "云模型调用失败")
    result["elapsedMs"] = result.get("elapsedMs") or int((time.monotonic() - state["started"]) * 1000)
    result["progress"] = {"contentChars": state.get("contentChars", 0), "reasoningChars": state.get("reasoningChars", 0)}
    return result


async def call_workbuddy_full(messages: list[dict[str, Any]], model: str, timeout_seconds: float = 240,
                              temperature: float = 0.2,
                              on_progress: Callable[[dict[str, Any]], None] | None = None,
                              stall_seconds: float | None = None, task: str = "chat",
                              json_mode: bool = True) -> dict[str, Any]:
    """Return the full bridge result, including usage and elapsed time.

    `task` labels the call in the local usage ledger so the dashboard can separate
    reading costs from chat costs.
    """
    payload: dict[str, Any] = {"mode": "chat", "model": model, "messages": messages,
                               "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    result = await _run_worker(payload, timeout_seconds=timeout_seconds, on_progress=on_progress,
                               stall_seconds=stall_seconds)
    usage = result.get("usage")
    if usage:
        with USAGE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": now_iso(), "model": result.get("model") or model,
                                     "task": task, "elapsed_ms": result.get("elapsedMs"), "usage": usage},
                                    ensure_ascii=False) + "\n")
    return result


async def call_workbuddy(messages: list[dict[str, Any]], model: str, timeout_seconds: float = 240,
                         temperature: float = 0.2,
                         on_progress: Callable[[dict[str, Any]], None] | None = None,
                         stall_seconds: float | None = None, task: str = "chat") -> str:
    result = await call_workbuddy_full(messages, model, timeout_seconds, temperature,
                                       on_progress, stall_seconds, task=task)
    return result.get("content") or ""


async def list_workbuddy_models() -> list[dict[str, Any]]:
    result = await _run_worker({"mode": "models"}, timeout_seconds=60)
    return result.get("models") or []
