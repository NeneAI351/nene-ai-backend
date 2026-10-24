"""Authenticated character API with strict owner and project scoping."""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

import database
from auth import get_authenticated_user_id
import character_store
from account_profile import ProfileUpdate

router = APIRouter(prefix="/api/characters", tags=["characters"])
MAX_PROFILE_BYTES = 16_384


class CharacterCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Optional[uuid.UUID] = None
    name: str = Field(min_length=1, max_length=120)
    project_id: Optional[uuid.UUID] = None
    profile: dict[str, Any] = Field(default_factory=dict)


class CharacterUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    project_id: Optional[uuid.UUID] = None
    profile: Optional[dict[str, Any]] = None


def _clean_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Character name cannot be empty.")
    return name


def _validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    # Reference bytes stay inside owner-protected PostgreSQL storage, never a
    # public asset URL. Apply the same bounded image validation as profile photos.
    description = dict(profile)
    reference = description.pop("referenceImage", None)
    if reference is not None:
        try:
            if not isinstance(reference, str):
                raise ValueError("Invalid reference image")
            ProfileUpdate(photo=reference)
        except ValueError:
            raise HTTPException(status_code=422, detail="Choose a PNG, JPEG or WebP reference under 400 KB.")
    try:
        encoded = json.dumps(description, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Character profile must be valid JSON.")
    if len(encoded.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise HTTPException(status_code=422, detail="Character profile exceeds the size limit.")
    return profile


def _database_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="NENE AI character storage is unavailable.")


@router.get("")
async def list_characters(
    project_id: Optional[uuid.UUID] = None,
    limit: int = Query(default=50, ge=1, le=100),
    user_id: str = Depends(get_authenticated_user_id),
):
    if not database.database_configured():
        raise _database_unavailable()
    try:
        items = await character_store.list_characters(
            user_id, str(project_id) if project_id else None, limit
        )
    except RuntimeError:
        raise _database_unavailable()
    # Filtering by user ID alone is insufficient for a project filter: verify that
    # a requested project is owned, so a foreign project is indistinguishable from absent.
    if project_id is not None:
        try:
            project = await database.fetch_one(
                "SELECT id FROM projects WHERE id = %s AND user_id = %s AND status = 'active'",
                (str(project_id), user_id),
            )
        except RuntimeError:
            raise _database_unavailable()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
    return {"items": items}


@router.post("", status_code=201)
async def create_character(
    body: CharacterCreate,
    user_id: str = Depends(get_authenticated_user_id),
):
    if not database.database_configured():
        raise _database_unavailable()
    name = _clean_name(body.name)
    profile = _validate_profile(body.profile)
    try:
        character = await character_store.create_character(
            user_id,
            str(body.id or uuid.uuid4()),
            str(body.project_id) if body.project_id else None,
            name,
            profile,
        )
    except ValueError:
        raise HTTPException(status_code=409, detail="This character save ID was already used. Reload your characters before saving again.")
    except RuntimeError:
        raise _database_unavailable()
    if character is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return character


@router.get("/{character_id}")
async def read_character(
    character_id: uuid.UUID,
    user_id: str = Depends(get_authenticated_user_id),
):
    if not database.database_configured():
        raise _database_unavailable()
    try:
        character = await character_store.get_character(user_id, str(character_id))
    except RuntimeError:
        raise _database_unavailable()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found.")
    return character


@router.patch("/{character_id}")
async def edit_character(
    character_id: uuid.UUID,
    body: CharacterUpdate,
    user_id: str = Depends(get_authenticated_user_id),
):
    if not database.database_configured():
        raise _database_unavailable()
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one character field to update.")
    if "name" in changes:
        if changes["name"] is None:
            raise HTTPException(status_code=422, detail="Character name cannot be null.")
        changes["name"] = _clean_name(changes["name"])
    if "profile" in changes:
        if changes["profile"] is None:
            raise HTTPException(status_code=422, detail="Character profile cannot be null.")
        changes["profile"] = _validate_profile(changes["profile"])
    try:
        character = await character_store.update_character(
            user_id,
            str(character_id),
            name=changes.get("name"),
            profile=changes.get("profile"),
            project_id=str(changes["project_id"]) if changes.get("project_id") else None,
            update_name="name" in changes,
            update_profile="profile" in changes,
            update_project="project_id" in changes,
        )
    except RuntimeError:
        raise _database_unavailable()
    if character is None:
        # Deliberately do not reveal whether the character or target project belongs
        # to a different account.
        raise HTTPException(status_code=404, detail="Character or project not found.")
    return character


@router.delete("/{character_id}", status_code=204)
async def remove_character(
    character_id: uuid.UUID,
    user_id: str = Depends(get_authenticated_user_id),
):
    if not database.database_configured():
        raise _database_unavailable()
    try:
        deleted = await character_store.delete_character(user_id, str(character_id))
    except RuntimeError:
        raise _database_unavailable()
    if not deleted:
        raise HTTPException(status_code=404, detail="Character not found.")
    return None

