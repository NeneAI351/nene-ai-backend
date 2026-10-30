"""Capability-aware provider selection for NENE AI.

This module intentionally distinguishes a provider being known from its adapter
being enabled for durable production generation. Do not route jobs to providers
whose adapters have not passed integration tests.
"""
from __future__ import annotations

import os
from typing import Any

PROVIDERS: dict[str, dict[str, Any]] = {
    "ltx": {
        "label": "LTX",
        "generation_types": ["text-to-video", "image-to-video", "audio-to-video"],
        "adapter_enabled": True,
        "required_env": ["LTX_API_KEY"],
        "strengths": ["fast iteration", "text-to-video", "image-to-video"],
    },
    "magic-hour": {
        "label": "Magic Hour",
        "generation_types": ["text-to-video", "image-to-video"],
        # New jobs remain locked behind an explicit deployment flag and a
        # configured account credit cost until real integration tests pass.
        "adapter_enabled": True,
        "required_env": ["MAGIC_HOUR_API_KEY"],
        "strengths": ["creative video workflows"],
    },
    "pixazo": {
        "label": "Pixazo",
        "generation_types": ["text-to-video", "image-to-video"],
        "adapter_enabled": False,
        "required_env": ["PIXAZO_API_KEY"],
        "strengths": ["multiple model choices"],
    },
}

ALIASES = {"magic hour": "magic-hour", "magic_hour": "magic-hour"}


def _key(value: str | None) -> str:
    key = (value or "auto").strip().lower()
    return ALIASES.get(key, key)


def provider_status() -> list[dict[str, Any]]:
    """Return safe public status; never expose credential values."""
    result = []
    for key, item in PROVIDERS.items():
        configured = all(os.getenv(name, "").strip() for name in item["required_env"])
        if key == 'magic-hour':
            try:
                from pricing import get_rate
                configured = configured and get_rate('magic-hour','minimax-h3','720p') is not None
            except ValueError:configured = False
            from magic_hour_video import enabled as backup_enabled
            configured = configured and backup_enabled()
        if key == 'ltx':
            configured = configured and os.getenv('NENE_LTX_VIDEO_ENABLED','true').strip().lower() == 'true'
        enabled = bool(item["adapter_enabled"] and configured)
        result.append({
            "id": key,
            "label": item["label"],
            "generation_types": list(item["generation_types"]),
            "available": enabled,
            "status": "available" if enabled else (
                "adapter_not_verified" if not item["adapter_enabled"] else "configuration_required"
            ),
            "strengths": list(item["strengths"]),
        })
    return result


def select_provider(
    *,
    mode: str = "auto",
    generation_type: str = "text-to-video",
    resolution: str = "720p",
    duration_seconds: int = 6,
    max_credits: int | None = None,
) -> dict[str, Any]:
    """Choose only an adapter that is enabled and configured.

    Selection is PRE-submission only. An accepted or uncertain generation never
    changes provider. Backup use still requires a fresh server quote/approval.
    """
    requested = _key(mode)
    if generation_type not in {"text-to-video", "image-to-video", "audio-to-video"}:
        raise ValueError("Unsupported generation type.")
    if (generation_type != "audio-to-video" and duration_seconds not in {6, 8, 10}) or (generation_type == "audio-to-video" and not 2 <= duration_seconds <= 10):
        raise ValueError("Duration must be one of 6, 8, or 10 seconds.")
    if requested != "auto" and requested not in PROVIDERS:
        raise ValueError("Choose Auto, Magic Hour, Pixazo, or LTX.")

    status = {p["id"]: p for p in provider_status()}
    candidates = [requested] if requested != "auto" else ["ltx", "magic-hour", "pixazo"]
    for provider in candidates:
        p = status[provider]
        if not p["available"] or generation_type not in p["generation_types"]:
            continue
        if provider == 'magic-hour' and resolution not in {'720p','1080p'}:continue
        # Pricing must be calculated by the authoritative pricing module after
        # selection. Never trust a browser-provided credit quote.
        return {
            "provider": provider,
            "model": 'minimax-h3' if provider == 'magic-hour' else 'ltx-2-5-fast',
            "label": p["label"],
            "mode": requested,
            "generation_type": generation_type,
            "resolution": resolution,
            "duration_seconds": duration_seconds,
            "requires_server_quote": True,
            "estimated_credits": None,
            "selection_reason": (
                "Best currently enabled compatible provider."
                if requested == "auto" else "Matches your explicit provider choice."
            ),
        }

    if requested != "auto":
        p = status[requested]
        if not p["available"]:
            raise ValueError(f"{p['label']} is not enabled for generation yet.")
        raise ValueError(f"{p['label']} does not support this generation type.")
    raise RuntimeError("No configured, verified video provider is currently available.")
