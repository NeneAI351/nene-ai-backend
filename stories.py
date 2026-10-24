"""Authenticated Story -> Video persistence API. No client may set an owner ID."""
from __future__ import annotations
import json, uuid
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastapi import Header
import media_storage
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
    generate_audio: bool = Field(default=True, strict=True)
    sound_direction: str = Field(default="", max_length=500)

    @field_validator("duration_seconds")
    @classmethod
    def supported_duration(cls, value: int) -> int:
        if value not in {6, 8, 10}:
            raise ValueError("Scene duration must be 6, 8, or 10 seconds.")
        return value

class ScenesSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[SceneInput] = Field(min_length=1,max_length=50)

class GenerationLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation_id: uuid.UUID

def _unavailable():
    return HTTPException(status_code=503,detail="Durable Story storage is unavailable.")

def _ensure_unique_scene_indexes(scenes: list[dict[str,Any]]) -> None:
    indexes=[scene["scene_index"] for scene in scenes]
    if len(indexes)!=len(set(indexes)):
        raise ValueError("Scene indexes must be unique.")
    if sorted(indexes)!=list(range(len(indexes))):
        raise ValueError("Scene indexes must be contiguous and start at zero.")

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
    try: _ensure_unique_scene_indexes(scenes)
    except ValueError as exc: raise HTTPException(status_code=422,detail=str(exc))
    if any(s["duration_seconds"] not in {6,8,10} for s in scenes):
        raise HTTPException(status_code=422,detail="Scene duration must be 6, 8, or 10 seconds.")
    scenes.sort(key=lambda s:s["scene_index"])
    try: story=await story_store.save_scenes(user_id,str(story_id),scenes)
    except ValueError as exc: raise HTTPException(status_code=409,detail=str(exc))
    except RuntimeError: raise _unavailable()
    if story is None: raise HTTPException(status_code=404,detail="Story not found.")
    return story

@router.post("/{story_id}/scenes/{scene_index}/generation",status_code=202)
async def attach_scene_generation(story_id: uuid.UUID,scene_index: int,body: GenerationLink,user_id: str=Depends(get_authenticated_user_id)):
    if not database.database_configured(): raise _unavailable()
    if scene_index<0 or scene_index>=50: raise HTTPException(status_code=422,detail="Invalid scene index.")
    try: linked=await story_store.link_scene_generation(user_id,str(story_id),scene_index,str(body.generation_id))
    except RuntimeError: raise _unavailable()
    if not linked: raise HTTPException(status_code=404,detail="Story scene or owned generation not found.")
    return {"ok":True,"story_id":str(story_id),"scene_index":scene_index,"generation_id":str(body.generation_id),"status":"queued"}

@router.delete("/{story_id}",status_code=204)
async def remove_story(story_id: uuid.UUID,user_id: str=Depends(get_authenticated_user_id)):
    if not database.database_configured(): raise _unavailable()
    try: deleted=await story_store.delete_story(user_id,str(story_id))
    except RuntimeError: raise _unavailable()
    if not deleted: raise HTTPException(status_code=404,detail="Story not found.")
    return None

@router.post("/{story_id}/assemble",status_code=202)
async def request_story_assembly(
    story_id: uuid.UUID,
    idempotency_key: str=Header(alias="Idempotency-Key",min_length=1,max_length=160),
    user_id: str=Depends(get_authenticated_user_id),
):
    if not database.database_configured(): raise _unavailable()
    try:
        job=await story_store.queue_assembly(user_id,str(story_id),idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc))
    except RuntimeError:
        raise _unavailable()
    if job is None: raise HTTPException(status_code=404,detail="Story not found.")
    if job.get("story_id") and job.get("story_id")!=str(story_id):
        raise HTTPException(status_code=409,detail="Idempotency key was already used for another story.")
    return {"story_id":str(story_id),"assembly_job_id":job.get("id"),"status":job.get("status"),"error_code":job.get("error_code")}

@router.get("/{story_id}/assembly")
async def story_assembly_status(story_id: uuid.UUID,user_id: str=Depends(get_authenticated_user_id)):
    if not database.database_configured(): raise _unavailable()
    try:
        story=await story_store.get_story(user_id,str(story_id))
        if story is None: raise HTTPException(status_code=404,detail="Story not found.")
        job=await story_store.get_assembly_job(user_id,str(story_id))
    except RuntimeError: raise _unavailable()
    return {"story_id":str(story_id),"story_status":story["status"],"assembly":job}

@router.get("/{story_id}/video")
async def story_video_playback(story_id: uuid.UUID,user_id: str=Depends(get_authenticated_user_id)):
    if not database.database_configured(): raise _unavailable()
    try:
        story=await database.fetch_one("""SELECT final_object_key FROM story_projects
          WHERE id=%s AND user_id=%s AND status='completed'""",(str(story_id),user_id))
    except RuntimeError: raise _unavailable()
    if not story: raise HTTPException(status_code=404,detail="Completed story video not found.")
    if not story.get("final_object_key"): raise HTTPException(status_code=404,detail="Final video is not ready.")
    try:
        url=await __import__("asyncio").to_thread(media_storage.create_signed_download_url,story["final_object_key"])
    except media_storage.MediaStorageNotConfigured:
        raise HTTPException(status_code=503,detail="Private video storage is unavailable.")
    except (ValueError,RuntimeError):
        raise HTTPException(status_code=503,detail="Private video playback URL could not be created.")
    return {"video_url":url,"expires_in":media_storage.SIGNED_URL_TTL_SECONDS,"content_type":"video/mp4"}

