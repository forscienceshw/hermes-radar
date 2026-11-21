"""One place that talks to whichever model the user picked.

Readings still go through `analyzer._llm_call`; this gateway exists for the
interactive assistant, where JSON mode must be off and the model list is a mix of
WorkBuddy catalogue models and the user's own OpenAI-compatible endpoints.
"""
from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlsplit

from ..net import post_json
from ..settings import get_secret, get_settings

DEFAULT_ACCOUNT = "default"


def accounts() -> list[dict[str, Any]]:
    """Configured chat accounts, always led by the reading model as `default`."""
    settings = get_settings()["llm"]
    result: list[dict[str, Any]] = [{
        "id": DEFAULT_ACCOUNT,
        "label": f"默认 · {settings.get('model') or '未配置'}",
        "provider": settings.get("provider", "openai_compatible"),
        "model": settings.get("model", ""),
        "base_url": settings.get("base_url", ""),
        "key_name": "llm_api_key" if settings.get("provider") == "openai_compatible" else "",
    }]
    seen = {result[0]["model"]}
    for account in settings.get("accounts") or []:
        if not isinstance(account, dict) or not account.get("model"):
            continue
        label = account.get("label") or account.get("model")
        result.append({
            "id": str(account.get("id") or account.get("model")),
            "label": label,
            "provider": account.get("provider") or "openai_compatible",
            "model": str(account["model"]),
            "base_url": account.get("base_url") or "",
            "key_name": account.get("key_name") or f"provider:{account.get('id') or account.get('model')}",
            "supports_images": bool(account.get("supports_images")),
        })
        seen.add(str(account["model"]))
    return result


def resolve(account_id: str | None) -> dict[str, Any]:
    items = accounts()
    if not account_id:
        return items[0]
    for item in items:
        if item["id"] == account_id or item["model"] == account_id:
            return item
    raise ValueError(f"没有名为 {account_id} 的模型账户；请在设置里添加")


def _endpoint(base_url: str) -> str:
    endpoint = (base_url or "").rstrip("/")
    if not endpoint:
        raise ValueError("该账户未填写 Base URL")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    return endpoint


async def complete(messages: list[dict[str, Any]], account_id: str | None = None, *,
                   temperature: float = 0.5, timeout_seconds: float = 240,
                   on_progress: Callable[[dict[str, Any]], None] | None = None,
                   task: str = "chat") -> dict[str, Any]:
    account = resolve(account_id)
    provider = account["provider"]
    started = time.monotonic()
    if provider == "workbuddy_cloud":
        from .workbuddy_llm import call_workbuddy_full
        result = await call_workbuddy_full(messages, account["model"], timeout_seconds, temperature,
                                            on_progress=on_progress, task=task, json_mode=False)
        return {"content": result.get("content") or "", "model": account["model"],
                "usage": result.get("usage") or {}, "elapsed_ms": result.get("elapsedMs") or 0,
                "account": account["id"], "label": account["label"]}
    if provider == "ollama":
        endpoint = _endpoint(account.get("base_url") or "http://127.0.0.1:11434/v1")
        parts = urlsplit(endpoint)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("Ollama 仅允许本机回环地址")
        key = ""
    else:
        endpoint = _endpoint(account.get("base_url") or "")
        key = get_secret(account.get("key_name") or "llm_api_key")
        if not key:
            raise RuntimeError(f"账户「{account['label']}」没有可用的 API Key，请在设置里填写")
    payload = {"model": account["model"], "messages": messages, "temperature": temperature}
    response, _ = await post_json(endpoint, payload,
                                  headers={"Authorization": f"Bearer {key}"} if key else None,
                                  timeout=timeout_seconds)
    try:
        content = response["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"模型返回格式异常：{str(response)[:300]}")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if provider == "openai_compatible":
        from .api_usage import record as record_api_usage
        record_api_usage(provider=provider, model=account["model"],
                         base_url=account.get("base_url") or "",
                         usage=response.get("usage") or {}, elapsed_ms=elapsed_ms,
                         task=task, account=account["id"], label=account["label"])
    return {"content": content, "model": account["model"], "usage": response.get("usage") or {},
            "elapsed_ms": elapsed_ms,
            "account": account["id"], "label": account["label"]}
