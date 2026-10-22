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
        "generation_types": ["text-to-video", "image-to-video"],
        "adapter_enabled": True,
        "required_env": ["LTX_API_KEY"],
        "strengths": ["fast iteration", "text-to-video", "image-to-video"],
    },
    "magic-hour": {
        "label": "Magic Hour",
        "generation_types": ["text-to-video", "image-to-video"],
        # Known provider, but its durable job submission/status/media adapter
        # has not yet been implemented and verified in this backend.
        "adapter_enabled": False,
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

    Auto currently chooses LTX when it is configured because it is the only
    durable adapter implemented in this backend. This is intentionally honest:
    adding provider names to the UI must never imply their adapters work.
    """
    requested = _key(mode)
    if generation_type not in {"text-to-video", "image-to-video"}:
        raise ValueError("Unsupported generation type.")
    if duration_seconds not in {6, 8, 10}:
        raise ValueError("Duration must be one of 6, 8, or 10 seconds.")
    if requested != "auto" and requested not in PROVIDERS:
        raise ValueError("Choose Auto, Magic Hour, Pixazo, or LTX.")

    status = {p["id"]: p for p in provider_status()}
    candidates = [requested] if requested != "auto" else ["ltx", "magic-hour", "pixazo"]
    for provider in candidates:
        p = status[provider]
        if not p["available"] or generation_type not in p["generation_types"]:
            continue
        # Pricing must be calculated by the authoritative pricing module after
        # selection. Never trust a browser-provided credit quote.
        return {
            "provider": provider,
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
