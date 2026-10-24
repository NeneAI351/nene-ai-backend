"""Durable Story assembly worker.

Run as a separate process with the same DATABASE_URL and private media-storage
configuration as the API. Scene clips must already be completed durable generation
outputs. Assembly itself does not submit provider jobs or charge credits.
"""
from __future__ import annotations
import asyncio, json, logging, math, os, subprocess, tempfile, uuid
from pathlib import Path
from typing import Any
import database
import media_storage
import story_store

logger=logging.getLogger("nene-ai.story-assembly")
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
MAX_SCENES=50
MAX_TOTAL_BYTES=2*1024*1024*1024
MAX_ASSEMBLY_SECONDS=1800

def _download_private_object(object_key: str, destination: str) -> int:
    if not isinstance(object_key,str) or not object_key.startswith("generations/") or not object_key.endswith("/video.mp4"):
        raise ValueError("invalid_scene_object_key")
    pieces=object_key.split("/")
    if len(pieces)!=3 or str(uuid.UUID(pieces[1]))!=pieces[1]:
        raise ValueError("invalid_scene_object_key")
    cfg=media_storage._storage_config()
    client=media_storage._client()
    response=client.get_object(Bucket=cfg["bucket"],Key=object_key)
    body=response["Body"]
    total=0
    try:
        with open(destination,"wb") as out:
            while True:
                chunk=body.read(1024*1024)
                if not chunk: break
                total+=len(chunk)
                if total>media_storage.MAX_VIDEO_BYTES:
                    raise ValueError("scene_video_too_large")
                out.write(chunk)
    finally:
        body.close()
    if total<12: raise ValueError("scene_video_empty")
    with open(destination,"rb") as f:
        if f.read(64)[4:8]!=b"ftyp": raise ValueError("scene_video_invalid_mp4")
    return total

def _normalize_scene(source: Path, target: Path, preserve_audio: bool = True) -> None:
    """Give every clip matching video/audio streams without discarding sound.

    Duration follows the video, not a possibly shorter/longer soundtrack. Missing
    audio gets silence, short audio is padded, and long audio is trimmed.
    """
    probe=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(source)],check=True,timeout=30,capture_output=True,text=True)
    data=json.loads(probe.stdout)
    video=next((stream for stream in data.get('streams',[]) if stream.get('codec_type')=='video'),None)
    if not video: raise ValueError('scene_video_stream_missing')
    duration=float(video.get('duration') or data.get('format',{}).get('duration') or 0)
    start=float(video.get('start_time') or 0)
    if not math.isfinite(duration) or not math.isfinite(start) or duration<=0 or duration>120:
        raise ValueError('scene_video_duration_invalid')
    has_audio=preserve_audio and any(stream.get('codec_type')=='audio' for stream in data.get('streams',[]))
    cmd=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-copyts','-i',str(source)]
    if not has_audio:cmd+=['-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=48000']
    cmd+=['-map','0:v:0','-map','0:a:0' if has_audio else '1:a:0',
          '-vf','setpts=PTS-STARTPTS,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p',
          '-af',f'asetpts=PTS-({start if has_audio else 0})/TB,aresample=48000:async=1:first_pts=0,apad',
          '-c:v','libx264','-preset','veryfast','-c:a','aac','-ar','48000','-ac','2',
          '-t',str(duration),'-movflags','+faststart',str(target)]
    subprocess.run(cmd,check=True,timeout=MAX_ASSEMBLY_SECONDS,capture_output=True)
    if not target.exists() or target.stat().st_size<12:raise RuntimeError('scene_normalization_failed')

def _assemble_and_upload(scene_keys: list[str], assembly_id: str, audio_flags: list[bool]|None = None) -> dict[str,Any]:
    if not scene_keys or len(scene_keys)>MAX_SCENES: raise ValueError("invalid_scene_count")
    with tempfile.TemporaryDirectory(prefix="nene-story-assembly-") as temp:
        root=Path(temp)
        local=[]; total=0
        for index,key in enumerate(scene_keys):
            target=root/f"scene-{index:03d}.mp4"
            total+=_download_private_object(key,str(target))
            if total>MAX_TOTAL_BYTES: raise ValueError("story_sources_too_large")
            local.append(target)
        # Normalize to H.264/AAC with matching stereo audio streams. No remote
        # URLs are passed to FFmpeg; source order stays exactly as persisted.
        normalized=[]
        for index,source in enumerate(local):
            target=root/f"normalized-{index:03d}.mp4"
            _normalize_scene(source,target, audio_flags[index] if audio_flags is not None else True)
            normalized.append(target)
        concat_file=root/"concat.txt"
        # All paths are generated local paths without apostrophes.
        concat_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in normalized),encoding="utf-8")
        output=root/"final.mp4"
        subprocess.run(["ffmpeg","-nostdin","-hide_banner","-loglevel","error","-y",
          "-f","concat","-safe","0","-i",str(concat_file),"-c","copy","-movflags","+faststart",str(output)],
          check=True,timeout=MAX_ASSEMBLY_SECONDS,capture_output=True)
        if not output.exists(): raise RuntimeError("story_assembly_output_missing")
        stored=media_storage.upload_video_file(str(output),assembly_id)
        return stored

async def _claim_job(worker_id: str) -> dict[str,Any]|None:
    if database._pool is None: return None
    async with database._pool.connection() as conn:
        async with conn.transaction():
            row=await (await conn.execute("""SELECT id::text,story_id::text,user_id::text
              FROM story_assembly_jobs
              WHERE status='queued' OR (status='running' AND lease_expires_at < NOW())
              ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED""")).fetchone()
            if not row:return None
            await conn.execute("""UPDATE story_assembly_jobs SET status='running',lease_owner=%s,
              lease_expires_at=NOW()+INTERVAL '10 minutes',attempts=attempts+1,updated_at=NOW()
              WHERE id=%s""",(worker_id,row["id"]))
            row["lease_owner"]=worker_id
            return row

async def _heartbeat(job_id: str, user_id: str, worker_id: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with database._pool.connection() as conn:
                await conn.execute("""UPDATE story_assembly_jobs SET lease_expires_at=NOW()+INTERVAL '10 minutes',
                  updated_at=NOW() WHERE id=%s AND user_id=%s AND status='running' AND lease_owner=%s""",
                  (job_id,user_id,worker_id))
                await conn.commit()
        except Exception:
            logger.exception("Assembly lease heartbeat failed for job %s",job_id)
        try:
            await asyncio.wait_for(stop.wait(),timeout=30)
        except asyncio.TimeoutError:
            pass

async def _process_unlocked(job: dict[str,Any]) -> None:
    job_id,story_id,user_id=job["id"],job["story_id"],job["user_id"]
    worker_id=job.get("lease_owner","")
    stop_heartbeat=asyncio.Event()
    heartbeat=asyncio.create_task(_heartbeat(job_id,user_id,worker_id,stop_heartbeat))
    try:
        # Lock the story and verify every scene has a durable private MP4 result.
        rows=await database.fetch_all("""SELECT s.scene_index,s.generate_audio,COALESCE(s.result_object_key,g.result->>'object_key') AS object_key,
          g.status AS generation_status
          FROM story_scenes s LEFT JOIN generations g ON g.id=s.generation_id AND g.user_id=s.user_id
          WHERE s.story_id=%s AND s.user_id=%s ORDER BY s.scene_index""",(story_id,user_id))
        if not rows or len(rows)>MAX_SCENES: raise ValueError("story_scene_count_invalid")
        if any(not r.get("object_key") or r.get("generation_status")!="succeeded" for r in rows):
            raise ValueError("story_scenes_not_all_succeeded")
        if [r["scene_index"] for r in rows]!=list(range(len(rows))):
            raise ValueError("story_scene_order_incomplete")
        stored=await asyncio.to_thread(_assemble_and_upload,[r["object_key"] for r in rows],job_id,[r.get("generate_audio",True) for r in rows])
        async with database._pool.connection() as conn:
            async with conn.transaction():
                # Owner-scoped finalization and job completion commit together.
                finalized=await (await conn.execute("""UPDATE story_assembly_jobs SET status='succeeded',
                  output_object_key=%s,error_code=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=NOW()
                  WHERE id=%s AND user_id=%s AND status='running' AND lease_owner=%s RETURNING id""",
                  (stored["object_key"],job_id,user_id,worker_id))).fetchone()
                if finalized:
                    await conn.execute("""UPDATE story_projects SET status='completed',final_object_key=%s,
                      error_code=NULL,updated_at=NOW() WHERE id=%s AND user_id=%s""",
                      (stored["object_key"],story_id,user_id))
    except Exception as exc:
        code=str(exc)[:120] if isinstance(exc,ValueError) else "story_assembly_failed"
        logger.exception("Story assembly job %s failed",job_id)
        async with database._pool.connection() as conn:
            async with conn.transaction():
                finalized=await (await conn.execute("""UPDATE story_assembly_jobs SET status='failed',error_code=%s,
                  lease_owner=NULL,lease_expires_at=NULL,updated_at=NOW()
                  WHERE id=%s AND user_id=%s AND status='running' AND lease_owner=%s RETURNING id""",
                  (code,job_id,user_id,worker_id))).fetchone()
                if finalized:
                    await conn.execute("""UPDATE story_projects SET status='failed',error_code=%s,updated_at=NOW()
                      WHERE id=%s AND user_id=%s AND status<>'completed'""",(code,story_id,user_id))
    finally:
        stop_heartbeat.set()
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

async def _process(job: dict[str,Any]) -> None:
    import account_privacy
    async with account_privacy.worker_guard(job.get('user_id')) as active:
        if active:await _process_unlocked(job)

async def run_worker() -> None:
    if not database.database_configured(): raise RuntimeError("DATABASE_URL is required")
    await database.start_database()
    await story_store.initialize_story_schema()
    import account_privacy
    await account_privacy.initialize_schema()
    worker_id=f"story-assembly-{uuid.uuid4()}"
    try:
        while True:
            job=await _claim_job(worker_id)
            if job is None:
                await asyncio.sleep(2)
                continue
            await _process(job)
    finally:
        await database.close_database()

if __name__=="__main__":
    asyncio.run(run_worker())

