"""Durable, owner-scoped Story -> Video project and scene persistence."""
from __future__ import annotations
import json
import uuid
from typing import Any
import database

MAX_SCENES = 50
MAX_SCENE_BYTES = 24_000

async def initialize_story_schema() -> None:
    if database._pool is None:
        return
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS story_projects (
              id UUID PRIMARY KEY,
              user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
              title TEXT NOT NULL,
              prompt TEXT NOT NULL,
              character_lock JSONB NOT NULL DEFAULT '{}'::jsonb,
              status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','generating','assembling','completed','failed')),
              final_generation_id UUID REFERENCES generations(id) ON DELETE SET NULL,
              final_object_key TEXT,
              error_code TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_story_projects_owner_updated ON story_projects(user_id, updated_at DESC)")
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS story_scenes (
              id UUID PRIMARY KEY,
              story_id UUID NOT NULL REFERENCES story_projects(id) ON DELETE CASCADE,
              user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              scene_index INTEGER NOT NULL CHECK (scene_index >= 0 AND scene_index < 50),
              prompt TEXT NOT NULL,
              duration_seconds INTEGER NOT NULL CHECK (duration_seconds IN (6,8,10)),
              status TEXT NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned','queued','generating','succeeded','failed')),
              generation_id UUID REFERENCES generations(id) ON DELETE SET NULL,
              result_object_key TEXT,
              error_code TEXT,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              UNIQUE(story_id, scene_index)
            )""")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_story_scenes_owner_story ON story_scenes(user_id, story_id, scene_index)")
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS story_assembly_jobs (
              id UUID PRIMARY KEY,
              story_id UUID NOT NULL REFERENCES story_projects(id) ON DELETE CASCADE,
              user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','succeeded','failed')),
              idempotency_key TEXT NOT NULL,
              output_object_key TEXT,
              error_code TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              UNIQUE(user_id, idempotency_key)
            )""")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_story_assembly_owner_story ON story_assembly_jobs(user_id, story_id, created_at DESC)")

async def create_story(user_id: str, title: str, prompt: str, project_id: str|None, character_lock: dict[str,Any]) -> dict[str,Any]:
    story_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        async with conn.transaction():
            if project_id:
                owned = await (await conn.execute("SELECT id FROM projects WHERE id=%s AND user_id=%s AND status='active' FOR KEY SHARE",(project_id,user_id))).fetchone()
                if not owned:
                    return None
            row = await (await conn.execute("""INSERT INTO story_projects
                (id,user_id,project_id,title,prompt,character_lock)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                RETURNING id::text,user_id::text,project_id::text,title,prompt,character_lock,status,final_generation_id::text,final_object_key,created_at,updated_at""",
                (story_id,user_id,project_id,title.strip(),prompt.strip(),json.dumps(character_lock)))).fetchone()
            return row

async def list_stories(user_id: str, limit: int=30) -> list[dict[str,Any]]:
    return await database.fetch_all("""SELECT id::text,project_id::text,title,prompt,status,
      final_generation_id::text,final_object_key,error_code,created_at,updated_at
      FROM story_projects WHERE user_id=%s ORDER BY updated_at DESC LIMIT %s""",(user_id,limit))

async def get_story(user_id: str, story_id: str) -> dict[str,Any]|None:
    story = await database.fetch_one("""SELECT id::text,user_id::text,project_id::text,title,prompt,
      character_lock,status,final_generation_id::text,final_object_key,error_code,created_at,updated_at
      FROM story_projects WHERE id=%s AND user_id=%s""",(story_id,user_id))
    if not story:
        return None
    story["scenes"] = await database.fetch_all("""SELECT s.id::text,s.scene_index,s.prompt,s.duration_seconds,
      CASE WHEN g.status='succeeded' THEN 'succeeded'
           WHEN g.status='failed' THEN 'failed'
           WHEN g.status IN ('submitting','submitted','running','submission_unknown') THEN 'generating'
           WHEN g.status='queued' THEN 'queued'
           ELSE s.status END AS status,
      s.generation_id::text,COALESCE(s.result_object_key,g.result->>'object_key') AS result_object_key,
      CASE WHEN g.status='failed' THEN COALESCE(g.error->>'code',s.error_code) ELSE s.error_code END AS error_code,
      s.created_at,s.updated_at
      FROM story_scenes s
      LEFT JOIN generations g ON g.id=s.generation_id AND g.user_id=s.user_id
      WHERE s.story_id=%s AND s.user_id=%s ORDER BY s.scene_index""",(story_id,user_id))
    return story

async def save_scenes(user_id: str, story_id: str, scenes: list[dict[str,Any]]) -> dict[str,Any]|None:
    async with database._pool.connection() as conn:
        async with conn.transaction():
            story = await (await conn.execute("SELECT id FROM story_projects WHERE id=%s AND user_id=%s FOR UPDATE",(story_id,user_id))).fetchone()
            if not story:
                return None
            for scene in scenes:
                scene_id = str(uuid.uuid4())
                await conn.execute("""INSERT INTO story_scenes
                    (id,story_id,user_id,scene_index,prompt,duration_seconds)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(story_id,scene_index) DO UPDATE SET
                      prompt=EXCLUDED.prompt,duration_seconds=EXCLUDED.duration_seconds,
                      status=CASE WHEN story_scenes.prompt=EXCLUDED.prompt
                        AND story_scenes.duration_seconds=EXCLUDED.duration_seconds
                        THEN story_scenes.status ELSE 'planned' END,
                      generation_id=CASE WHEN story_scenes.prompt=EXCLUDED.prompt
                        AND story_scenes.duration_seconds=EXCLUDED.duration_seconds
                        THEN story_scenes.generation_id ELSE NULL END,
                      result_object_key=CASE WHEN story_scenes.prompt=EXCLUDED.prompt
                        AND story_scenes.duration_seconds=EXCLUDED.duration_seconds
                        THEN story_scenes.result_object_key ELSE NULL END,
                      error_code=NULL,updated_at=NOW()""",
                    (scene_id,story_id,user_id,scene["scene_index"],scene["prompt"],scene["duration_seconds"]))
            await conn.execute("UPDATE story_projects SET updated_at=NOW() WHERE id=%s AND user_id=%s",(story_id,user_id))
    return await get_story(user_id,story_id)

async def link_scene_generation(user_id: str, story_id: str, scene_index: int, generation_id: str) -> bool:
    async with database._pool.connection() as conn:
        async with conn.transaction():
            owned = await (await conn.execute("SELECT id FROM generations WHERE id=%s AND user_id=%s",(generation_id,user_id))).fetchone()
            if not owned:
                return False
            row = await (await conn.execute("""UPDATE story_scenes SET generation_id=%s,status='queued',updated_at=NOW()
                WHERE story_id=%s AND user_id=%s AND scene_index=%s RETURNING id""",(generation_id,story_id,user_id,scene_index))).fetchone()
            if row:
                await conn.execute("UPDATE story_projects SET status='generating',updated_at=NOW() WHERE id=%s AND user_id=%s",(story_id,user_id))
            return bool(row)

async def delete_story(user_id: str, story_id: str) -> bool:
    row = await database.fetch_one("DELETE FROM story_projects WHERE id=%s AND user_id=%s RETURNING id",(story_id,user_id))
    return row is not None

async def queue_assembly(user_id: str, story_id: str, idempotency_key: str) -> dict[str,Any]|None:
    key=idempotency_key.strip()
    if not key or len(key)>160:
        raise ValueError("Invalid idempotency key.")
    async with database._pool.connection() as conn:
        async with conn.transaction():
            story=await (await conn.execute("""SELECT id::text,status,final_object_key
              FROM story_projects WHERE id=%s AND user_id=%s FOR UPDATE""",(story_id,user_id))).fetchone()
            if not story:
                return None
            if story.get("status")=="completed" and story.get("final_object_key"):
                return {"story_id":story_id,"status":"succeeded","output_object_key":story["final_object_key"]}
            existing=await (await conn.execute("""SELECT id::text,story_id::text,status,output_object_key,error_code
              FROM story_assembly_jobs WHERE user_id=%s AND idempotency_key=%s""",(user_id,key))).fetchone()
            if existing:
                if existing["story_id"] != story_id:
                    raise ValueError("Idempotency key was already used for another story.")
                return existing
            scenes=await (await conn.execute("""SELECT s.scene_index,g.status AS generation_status,
              COALESCE(s.result_object_key,g.result->>'object_key') AS object_key
              FROM story_scenes s LEFT JOIN generations g ON g.id=s.generation_id AND g.user_id=s.user_id
              WHERE s.story_id=%s AND s.user_id=%s ORDER BY s.scene_index FOR UPDATE OF s""",(story_id,user_id))).fetchall()
            if not scenes or len(scenes)>MAX_SCENES:
                raise ValueError("Story has no valid scene list.")
            if [r["scene_index"] for r in scenes]!=list(range(len(scenes))):
                raise ValueError("Story scenes must be complete and ordered.")
            if any(r["generation_status"]!="succeeded" or not r["object_key"] for r in scenes):
                raise ValueError("Every scene must finish successfully before assembly.")
            job_id=str(uuid.uuid4())
            row=await (await conn.execute("""INSERT INTO story_assembly_jobs
              (id,story_id,user_id,status,idempotency_key)
              VALUES (%s,%s,%s,'queued',%s)
              ON CONFLICT(user_id,idempotency_key) DO NOTHING
              RETURNING id::text,status,output_object_key,error_code""",
              (job_id,story_id,user_id,key))).fetchone()
            if not row:
                row=await (await conn.execute("""SELECT id::text,story_id::text,status,output_object_key,error_code
                  FROM story_assembly_jobs WHERE user_id=%s AND idempotency_key=%s""",(user_id,key))).fetchone()
            await conn.execute("UPDATE story_projects SET status='assembling',error_code=NULL,updated_at=NOW() WHERE id=%s AND user_id=%s",(story_id,user_id))
            return {"story_id":story_id,**row}

async def get_assembly_job(user_id: str, story_id: str) -> dict[str,Any]|None:
    return await database.fetch_one("""SELECT id::text,status,output_object_key,error_code,created_at,updated_at
      FROM story_assembly_jobs WHERE story_id=%s AND user_id=%s
      ORDER BY created_at DESC LIMIT 1""",(story_id,user_id))
