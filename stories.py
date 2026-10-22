"""Authenticated Story -> Video persistence API. No client may set an owner ID."""
from __future__ import annotations
import json, uuid
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
import database
from auth import get_authenticated_user_id
import story_store

router = APIRouter(prefix="/api/stories", tags=["stories"])

class StoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1,max_length=160)
    prompt: str = Field(min_length=1,max_length=12000)
    project_id: uuid.UUID|None = None
    character_lock: dict[str,Any] = Field(default_factory=dict)

class SceneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_index: int = Field(ge=0,lt=50)
    prompt: str = Field(min_length=1,max_length=12000)
    duration_seconds: int = Field(default=6)

    @field_validator("duration_seconds")
    @classmethod
    def supported_duration(cls, value: int) -> int:
        if value not in {6, 8, 10}:
            raise ValueError("Scene duration must be 6, 8, or 10 seconds.")
        return value

class ScenesSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[SceneInput] = Field(min_length=1,max_length=50)

def _unavailable():
    return HTTPException(status_code=503,detail="Durable Story storage is unavailable.")

@router.get("")
async def list_stories(limit: int=Query(default=30,ge=1,le=100), user_id: str=Depends(get_authenticated_user_id)):
    if not database.database_configured(): raise _unavailable()
    try: return {"items":await story_store.list_stories(user_id,limit)}
    except RuntimeError: raise _unavailable()

@router.post("",status_code=201)
async def create_story(body: StoryCreate,user_id: str=Depends(get_authenticated_user_id)):
    if not database.database_configured(): raise _unavailable()
    title=body.title.strip(); prompt=body.prompt.strip()
    if not title or not prompt: raise HTTPException(status_code=422,detail="Title and prompt cannot be blank.")
    try:
        lock=json.dumps(body.character_lock,allow_nan=False,separators=(",",":"))
        if len(lock.encode())>16000: raise HTTPException(status_code=422,detail="Character lock is too large.")
        story=await story_store.create_story(user_id,title,prompt,str(body.project_id) if body.project_id else None,body.character_lock)
    except (TypeError,ValueError): raise HTTPException(status_code=422,detail="Character lock must be valid bounded JSON.")
    except RuntimeError: raise _unavailable()
    if story is None: raise HTTPException(status_code=404,detail="Project not found.")
    return story

@router.get("/{story_id}")
async def read_story(story_id: uuid.UUID,user_id: str=Depends(get_authenticated_user_id)):
    if not database.database_configured(): raise _unavailable()
    try: story=await story_store.get_story(user_id,str(story_id))
    except RuntimeError: raise _unavailable()
    if story is None: raise HTTPException(status_code=404,detail="Story not found.")
    return story

@router.put("/{story_id}/scenes")
async def save_scenes(story_id: uuid.UUID,body: ScenesSave,user_id: str=Depends(get_authenticated_user_id)):
    if not database.database_configured(): raise _unavailable()
    scenes=[s.model_dump() for s in body.scenes]
    indexes=[s["scene_index"] for s in scenes]
    if len(indexes)!=len(set(indexes)): raise HTTPException(status_code=422,detail="Scene indexes must be unique.")
    if any(s["duration_seconds"] not in {6,8,10} for s in scenes):
        raise HTTPException(status_code=422,detail="Scene duration must be 6, 8, or 10 seconds.")
    scenes.sort(key=lambda s:s["scene_index"])
    try: story=await story_store.save_scenes(user_id,str(story_id),scenes)
    except RuntimeError: raise _unavailable()
    if story is None: raise HTTPException(status_code=404,detail="Story not found.")
    return story

@router.post("/{story_id}/scenes/{scene_index}/generation",status_code=202)
async def attach_scene_generation(story_id: uuid.UUID,scene_index: int,generation_id: uuid.UUID,user_id: str=Depends(get_authenticated_user_id)):
    if not database.database_configured(): raise _unavailable()
    if scene_index<0 or scene_index>=50: raise HTTPException(status_code=422,detail="Invalid scene index.")
    try: linked=await story_store.link_scene_generation(user_id,str(story_id),scene_index,str(generation_id))
    except RuntimeError: raise _unavailable()
    if not linked: raise HTTPException(status_code=404,detail="Story scene or owned generation not found.")
    return {"ok":True,"story_id":str(story_id),"scene_index":scene_index,"generation_id":str(generation_id),"status":"queued"}

@router.delete("/{story_id}",status_code=204)
async def remove_story(story_id: uuid.UUID,user_id: str=Depends(get_authenticated_user_id)):
    if not database.database_configured(): raise _unavailable()
    try: deleted=await story_store.delete_story(user_id,str(story_id))
    except RuntimeError: raise _unavailable()
    if not deleted: raise HTTPException(status_code=404,detail="Story not found.")
    return None
