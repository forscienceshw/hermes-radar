"""Live connectivity checks used by the setup wizard's "测试" buttons.

These are deliberately cheap: the WorkBuddy path only lists models (no tokens),
an OpenAI-compatible endpoint is asked for /models first and only falls back to a
one-token completion, and Ollama is a plain GET.  Nothing here writes settings.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

# A local proxy (Clash and friends) answers 127.0.0.1 requests with a 502 and
# makes every provider look broken; these probes always talk to the host直接.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _json_request(url: str, payload: dict[str, Any] | None = None,
                  headers: dict[str, str] | None = None, timeout: float = 15.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers or {},
                                     method="POST" if data is not None else "GET")
    with _OPENER.open(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8", "replace"))


def _normalise(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base


def _explain(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return f"HTTP {exc.code}：{body or exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"连不上：{exc.reason}"
    return f"{type(exc).__name__}: {exc}"


async def test_llm(provider: str, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    started = time.monotonic()
    if provider == "workbuddy_cloud":
        from . import workbuddy_direct
        from .workbuddy_llm import list_workbuddy_models

        try:
            models = await list_workbuddy_models()
        except Exception:                 # noqa: BLE001 - 目录拿不到不影响连通性验证
            models = []
        try:
            # 真正说明问题的是能不能跑通一次请求，而不是目录里有多少条。
            hit = await workbuddy_direct.probe(model or "glm-5.3-flash", timeout_seconds=45)
        except Exception as exc:          # noqa: BLE001 - 失败原因要原样告诉用户
            return {"ok": False, "message": f"WorkBuddy 点数不可用：{_explain(exc)}",
                    "seconds": round(time.monotonic() - started, 2)}
        ids = [item.get("id") for item in models][:8] or [hit["model"]]
        return {"ok": True,
                "message": f"已连接 WorkBuddy 点数（{hit['model']} 实测 {hit['seconds']} 秒）",
                "models": ids, "seconds": round(time.monotonic() - started, 2)}

    base = _normalise(base_url)
    if not base:
        return {"ok": False, "message": "先填 Base URL"}

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if provider == "ollama":
        try:
            status, payload = _json_request(f"{base}/models", headers=headers, timeout=8)
            names = [item.get("id") or item.get("name") for item in (payload.get("data") or payload.get("models") or [])]
            return {"ok": True, "message": f"Ollama 在线，{len(names)} 个本地模型", "models": names[:8],
                    "seconds": round(time.monotonic() - started, 2)}
        except Exception as exc:
            return {"ok": False, "message": f"连不上 Ollama：{_explain(exc)}（先确认 ollama serve 已启动）",
                    "seconds": round(time.monotonic() - started, 2)}

    if not api_key:
        return {"ok": False, "message": "这个接口需要 API Key"}
    if not model:
        return {"ok": False, "message": "先填模型名"}

    try:
        _, payload = _json_request(f"{base}/models", headers=headers, timeout=12)
        names = [item.get("id") for item in (payload.get("data") or [])]
        if names:
            hit = model in names
            return {"ok": True,
                    "message": (f"接口可用，{len(names)} 个模型" if hit else f"接口可用，但列表里没有 {model}"),
                    "models": names[:8], "model_known": hit,
                    "seconds": round(time.monotonic() - started, 2)}
    except Exception:
        pass  # 不少兼容端点没有 /models，直接试一次最小补全

    try:
        status, payload = _json_request(
            f"{base}/chat/completions",
            {"model": model, "messages": [{"role": "user", "content": "ping"}],
             "max_tokens": 1, "stream": False},
            headers=headers, timeout=30)
        usage = payload.get("usage") or {}
        return {"ok": True, "message": f"模型 {model} 已响应（消耗 {usage.get('total_tokens', '少量')} tokens）",
                "seconds": round(time.monotonic() - started, 2)}
    except Exception as exc:
        return {"ok": False, "message": f"调用失败：{_explain(exc)}",
                "seconds": round(time.monotonic() - started, 2)}


def test_wechat() -> dict[str, Any]:
    """Try to obtain the database key once, without reading any message."""
    started = time.monotonic()
    try:
        from . import wechat_self_archive as relay
        wxid, root, key = relay._account_and_key()
    except Exception as exc:
        return {"ok": False, "message": str(exc), "seconds": round(time.monotonic() - started, 2)}
    return {"ok": True, "message": f"已拿到密钥，可以只读 {wxid} 的数据库",
            "wxid": wxid, "db_root": str(root), "key_tail": f"…{key[-6:]}",
            "seconds": round(time.monotonic() - started, 2)}
