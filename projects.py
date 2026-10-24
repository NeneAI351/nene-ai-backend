"""Authenticated, owner-scoped project API."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import database
from auth import get_authenticated_user_id
import project_store

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)


def _clean_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Project name cannot be empty.")
    return name


def _database_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="NENE AI project storage is unavailable.")


@router.get("")
async def list_projects(
    limit: int = Query(default=50, ge=1, le=100),
    user_id: str = Depends(get_authenticated_user_id),
):
    if not database.database_configured():
        raise _database_unavailable()
    try:
        return {"items": await project_store.list_projects(user_id, limit)}
    except RuntimeError:
        raise _database_unavailable()


@router.post("", status_code=201)
async def create_project(
    body: ProjectCreate,
    user_id: str = Depends(get_authenticated_user_id),
):
    if not database.database_configured():
        raise _database_unavailable()
    name = _clean_name(body.name)
    try:
        return await project_store.create_project(
            user_id, str(uuid.uuid4()), name, body.description
        )
    except RuntimeError:
        raise _database_unavailable()


@router.get("/{project_id}")
async def read_project(
    project_id: uuid.UUID,
    user_id: str = Depends(get_authenticated_user_id),
):
    if not database.database_configured():
        raise _database_unavailable()
    try:
        project = await project_store.get_project(user_id, str(project_id))
    except RuntimeError:
        raise _database_unavailable()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


@router.patch("/{project_id}")
async def edit_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    user_id: str = Depends(get_authenticated_user_id),
):
    if not database.database_configured():
        raise _database_unavailable()
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one project field to update.")
    if "name" in changes:
        if changes["name"] is None:
            raise HTTPException(status_code=422, detail="Project name cannot be null.")
        changes["name"] = _clean_name(changes["name"])
    try:
        project = await project_store.update_project(
            user_id,
            str(project_id),
            name=changes.get("name"),
            description=changes.get("description"),
            update_name="name" in changes,
            update_description="description" in changes,
        )
    except RuntimeError:
        raise _database_unavailable()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


@router.delete("/{project_id}", status_code=204)
async def remove_project(
    project_id: uuid.UUID,
    user_id: str = Depends(get_authenticated_user_id),
):
    if not database.database_configured():
        raise _database_unavailable()
    try:
        deleted = await project_store.delete_project(user_id, str(project_id))
    except RuntimeError:
        raise _database_unavailable()
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found.")
    return None
