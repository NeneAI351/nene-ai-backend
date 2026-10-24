"""Public provider catalog and safe provider-selection preview endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import provider_routing

router = APIRouter(prefix="/api/providers", tags=["providers"])


class ProviderSelectionRequest(BaseModel):
    mode: str = Field(default="auto", max_length=40)
    generation_type: Literal["text-to-video", "image-to-video", "audio-to-video"] = "text-to-video"
    resolution: str = Field(default="720p", max_length=12)
    duration_seconds: int = Field(default=6, ge=1, le=20)
    max_credits: int | None = Field(default=None, ge=1)


@router.get("")
async def list_providers():
    """Expose only safe availability metadata; no API keys or secret config."""
    return {"providers": provider_routing.provider_status(), "default_mode": "auto"}


@router.post("/select")
async def preview_provider_selection(request: ProviderSelectionRequest):
    """Preview routing only; this endpoint never submits a billable generation."""
    try:
        selection = provider_routing.select_provider(
            mode=request.mode,
            generation_type=request.generation_type,
            resolution=request.resolution,
            duration_seconds=request.duration_seconds,
            max_credits=request.max_credits,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "selection": selection,
        "notice": "This is a routing preview, not a credit quote or generation request.",
    }
