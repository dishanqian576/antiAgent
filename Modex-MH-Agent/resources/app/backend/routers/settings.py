"""API settings management."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Dict

from fastapi import APIRouter
from pydantic import BaseModel

from services.llm_client import test_connection
from services.state_store import get_all_settings, save_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])

SENSITIVE_KEYS = {
    "executor_api_key",
    "reviewer_api_key",
    "editor_ai_api_key",
    "gpt_image_api_key",
    "minimax_api_key",
    "gemini_api_key",
}


class SettingsUpdate(BaseModel):
    settings: Dict[str, str]


def mask_value(key: str, value: str) -> str:
    if key in SENSITIVE_KEYS and len(value) > 8:
        return value[:4] + "*" * (len(value) - 8) + value[-4:]
    return value


@router.get("")
async def get_settings():
    raw = await get_all_settings()
    return {"settings": {key: mask_value(key, value) for key, value in raw.items()}}


@router.put("")
async def update_settings(body: SettingsUpdate):
    to_save: Dict[str, str] = {}
    for key, value in body.settings.items():
        if key in SENSITIVE_KEYS and "*" in value:
            continue
        to_save[key] = value
    if to_save:
        await save_settings(to_save)
    return {"status": "ok", "saved": len(to_save)}


@router.post("/test/{agent}")
async def test_agent_connection(agent: str):
    return await test_connection(agent)


@router.get("/detect-claude")
async def detect_claude():
    candidates = [
        "claude",
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "claude.cmd"),
        r"C:\Program Files\nodejs\claude.cmd",
    ]
    seen = set()
    results = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                shell=False,
            )
            version = (proc.stdout or proc.stderr or "").strip()
            if proc.returncode == 0:
                results.append({"path": candidate, "version": version or "ok"})
        except Exception:
            continue
    return {"recommended": results[0]["path"] if results else None, "candidates": results}
