"""LLM client helpers for OpenAI-compatible and Anthropic-compatible APIs."""
from __future__ import annotations

import asyncio
import base64
import http.client
import json
import logging
import socket
import ssl
from pathlib import Path
from typing import Dict
from urllib.parse import urlparse

from services.state_store import get_all_settings

log = logging.getLogger(__name__)

AGENT_KEYS = {
    "executor": ("executor_base_url", "executor_api_key", "executor_model_id"),
    "reviewer": ("reviewer_base_url", "reviewer_api_key", "reviewer_model_id"),
    "editor_ai": ("editor_ai_base_url", "editor_ai_api_key", "editor_ai_model_id"),
}

ENV_MAPPING = {
    "executor_api_key": "ANTHROPIC_API_KEY",
    "executor_base_url": "ANTHROPIC_BASE_URL",
    "executor_model_id": "EXECUTOR_MODEL_ID",
    "reviewer_api_key": "OPENAI_API_KEY",
    "reviewer_base_url": "OPENAI_BASE_URL",
    "reviewer_model_id": "REVIEWER_MODEL_ID",
    "editor_ai_api_key": "EDITOR_AI_API_KEY",
    "editor_ai_base_url": "EDITOR_AI_BASE_URL",
    "editor_ai_model_id": "EDITOR_AI_MODEL_ID",
    "minimax_api_key": "MINIMAX_API_KEY",
    "minimax_group_id": "MINIMAX_GROUP_ID",
    "gemini_api_key": "GEMINI_API_KEY",
    "claude_bin": "CLAUDE_BIN",
    "gpt_image_api_key": "GPT_IMAGE_API_KEY",
    "gpt_image_base_url": "GPT_IMAGE_BASE_URL",
}


def _is_anthropic_base(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return "anthropic" in parsed.path.strip("/").lower()


def _request_json(base_url: str, path: str, payload: Dict, headers: Dict[str, str], timeout: int) -> Dict:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"无法解析 Base URL: {base_url}")

    base_path = parsed.path.rstrip("/")
    request_path = f"{base_path}{path}"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        **headers,
    }

    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    kwargs = {"timeout": timeout}
    if parsed.scheme == "https":
        kwargs["context"] = ssl.create_default_context()

    log.info("[LLM] %s://%s:%s%s", parsed.scheme, parsed.hostname, port, request_path)
    conn = conn_cls(parsed.hostname, port, **kwargs)
    try:
        conn.request("POST", request_path, body, request_headers)
        res = conn.getresponse()
        text = res.read().decode("utf-8", "replace")
        if res.status >= 400:
            raise RuntimeError(f"HTTP {res.status}: {text[:1000]}")
        return json.loads(text)
    except socket.timeout:
        raise RuntimeError(f"请求超时（{timeout}秒），请检查网络连接或稍后重试")
    except socket.gaierror:
        raise RuntimeError("无法解析域名，请检查 Base URL 是否正确")
    except ConnectionRefusedError:
        raise RuntimeError("连接被拒绝，请检查 Base URL 和端口是否正确")
    finally:
        conn.close()


def _extract_anthropic_text(data: Dict) -> str:
    parts = []
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


def _call_llm_sync(base_url: str, api_key: str, model_id: str, prompt: str, timeout: int) -> str:
    if _is_anthropic_base(base_url):
        data = _request_json(
            base_url,
            "/v1/messages",
            {
                "model": model_id,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            },
            {
                "x-api-key": api_key,
                "Authorization": f"Bearer {api_key}",
                "anthropic-version": "2023-06-01",
            },
            timeout,
        )
        text = _extract_anthropic_text(data)
        if not text:
            raise RuntimeError(f"响应格式错误: {json.dumps(data, ensure_ascii=False)[:500]}")
        return text

    data = _request_json(
        base_url,
        "/v1/chat/completions",
        {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
        },
        {"Authorization": f"Bearer {api_key}"},
        timeout,
    )
    try:
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RuntimeError(f"响应格式错误: {json.dumps(data, ensure_ascii=False)[:500]}") from exc


async def call_llm(agent: str, prompt: str, timeout: int = 300) -> str:
    """Call an LLM from settings."""
    if agent not in AGENT_KEYS:
        raise ValueError(f"未知 agent: {agent}")
    settings = await get_all_settings()
    base_key, api_key_key, model_key = AGENT_KEYS[agent]
    base_url = settings.get(base_key, "").strip()
    api_key = settings.get(api_key_key, "").strip()
    model_id = settings.get(model_key, "").strip()
    if not api_key:
        raise RuntimeError(f"未配置 {agent} 的 API Key，请先在设置页面配置")
    if not base_url:
        raise RuntimeError(f"未配置 {agent} 的 Base URL，请先在设置页面配置")
    if not model_id:
        model_id = "gpt-4"
    return await asyncio.to_thread(_call_llm_sync, base_url, api_key, model_id, prompt, timeout)


async def test_connection(agent: str) -> Dict:
    """Test API connectivity."""
    try:
        message = await call_llm(agent, "Say hello in one word.", timeout=30)
        return {"ok": True, "message": message[:200], "agent": agent}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "agent": agent}


async def get_env_for_subprocess() -> Dict[str, str]:
    """Build environment variables for Claude Code and helper subprocesses."""
    settings = await get_all_settings()
    env: Dict[str, str] = {}
    for settings_key, env_var in ENV_MAPPING.items():
        value = settings.get(settings_key, "").strip()
        if value:
            env[env_var] = value

    executor_key = settings.get("executor_api_key", "").strip()
    executor_base = settings.get("executor_base_url", "").strip()
    executor_model = settings.get("executor_model_id", "").strip()
    if executor_key:
        env["ANTHROPIC_API_KEY"] = executor_key
        env["ANTHROPIC_AUTH_TOKEN"] = executor_key
    if executor_base:
        env["ANTHROPIC_BASE_URL"] = executor_base
    if executor_model:
        env["ANTHROPIC_MODEL"] = executor_model
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = executor_model
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = executor_model
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = executor_model
        env["EXECUTOR_MODEL_ID"] = executor_model

    return env


async def describe_image(image_path: str, context: str = "") -> str:
    """Describe an image with the editor model when available."""
    settings = await get_all_settings()
    base_url = settings.get("editor_ai_base_url", "").strip()
    api_key = settings.get("editor_ai_api_key", "").strip()
    model_id = settings.get("editor_ai_model_id", "").strip() or "gpt-4o"
    if not base_url or not api_key:
        raise RuntimeError("未配置编辑器 AI，请先在设置页面配置")

    path = Path(image_path)
    data_url = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    prompt = context or "Describe this image briefly."

    if _is_anthropic_base(base_url):
        payload = {
            "model": model_id,
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": data_url.split(",", 1)[1],
                            },
                        },
                    ],
                }
            ],
        }
        result = await asyncio.to_thread(
            _request_json,
            base_url,
            "/v1/messages",
            payload,
            {
                "x-api-key": api_key,
                "Authorization": f"Bearer {api_key}",
                "anthropic-version": "2023-06-01",
            },
            120,
        )
        return _extract_anthropic_text(result)

    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    result = await asyncio.to_thread(
        _request_json,
        base_url,
        "/v1/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
        120,
    )
    return result["choices"][0]["message"]["content"]
