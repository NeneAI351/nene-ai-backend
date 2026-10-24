"""Bounded speech uploads. Only private owner-scoped IDs cross the billing API."""
from __future__ import annotations
import asyncio,base64,math,shutil,subprocess,tempfile,uuid,wave
from pathlib import Path
from functools import lru_cache
from fastapi import APIRouter,Depends,HTTPException,Request
from pydantic import BaseModel,ConfigDict,Field
import database,media_storage
from auth import get_authenticated_user_id
from rate_limits import enforce_rate_limit

router=APIRouter(prefix='/api/speech-assets',tags=['speech'])
MAX_BYTES=8*1024*1024

@lru_cache(maxsize=1)
def ffmpeg_executable():
    system=shutil.which('ffmpeg')
    if system:return system
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError,RuntimeError):return None


async def initialize_schema():
    if database._pool is None:return
    await database.execute('''CREATE TABLE IF NOT EXISTS speech_assets (
      id UUID PRIMARY KEY,user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      object_key TEXT NOT NULL UNIQUE,duration_seconds NUMERIC(8,4) NOT NULL,
      billable_seconds INTEGER NOT NULL CHECK (billable_seconds BETWEEN 1 AND 60),
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',())
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute('ALTER TABLE speech_assets DROP CONSTRAINT IF EXISTS speech_assets_billable_seconds_check')
            await conn.execute('ALTER TABLE speech_assets ADD CONSTRAINT speech_assets_billable_seconds_check CHECK (billable_seconds BETWEEN 1 AND 60)')
    await database.execute('ALTER TABLE speech_assets ENABLE ROW LEVEL SECURITY',())

class SpeechUpload(BaseModel):
    model_config=ConfigDict(extra='forbid')
    data_uri:str=Field(max_length=12*1024*1024)

def normalize(raw:bytes,max_seconds:float=10,min_seconds:float=2)->tuple[bytes,float,int]:
    if not raw or len(raw)>MAX_BYTES:raise ValueError('Choose an audio recording up to 8 MB.')
    valid=(raw[:4] in {b'RIFF',b'OggS'} or raw[:3]==b'ID3' or (len(raw)>8 and raw[4:8]==b'ftyp') or (len(raw)>2 and raw[0]==255 and raw[1]&224==224))
    if not valid:raise ValueError('Choose a WAV, MP3, M4A or OGG recording.')
    with tempfile.TemporaryDirectory(prefix='nene-speech-') as temp:
        source=Path(temp)/'source';output=Path(temp)/'speech.wav';source.write_bytes(raw)
        subprocess.run([ffmpeg_executable() or 'ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',str(source),'-map','0:a:0','-vn','-t',str(max_seconds+.1),'-ac','1','-ar','48000','-c:a','pcm_s16le',str(output)],check=True,capture_output=True,timeout=20)
        with wave.open(str(output)) as audio:duration=audio.getnframes()/audio.getframerate()
        if not min_seconds<=duration<=max_seconds:raise ValueError('Use a recording between 2 and 10 seconds. Longer recordings must be split into scenes.')
        return output.read_bytes(),round(duration,4),math.ceil(duration)

def object_key(owner,asset):return f'source-speech/{uuid.UUID(str(owner))}/{uuid.UUID(str(asset))}.wav'

def signed_url(key):
    parts=key.split('/')
    if len(parts)!=3 or parts[0]!='source-speech' or not parts[2].endswith('.wav'):raise ValueError('Invalid speech asset key')
    if object_key(parts[1],parts[2][:-4])!=key:raise ValueError('Invalid speech asset key')
    return media_storage._client().generate_presigned_url('get_object',Params={'Bucket':media_storage._storage_config()['bucket'],'Key':key,'ResponseContentType':'audio/wav'},ExpiresIn=300)

@router.post('',status_code=201)
async def upload(body:SpeechUpload,request:Request,owner:str=Depends(get_authenticated_user_id)):
    await enforce_rate_limit(request,bucket='speech-upload',limit=6,window_seconds=60)
    if not database.database_configured() or not ffmpeg_executable():raise HTTPException(503,'Speech uploads are temporarily unavailable.')
    try:
        header,encoded=body.data_uri.split(',',1)
        if not header.startswith('data:audio/') or not header.endswith(';base64') or len(encoded)>((MAX_BYTES+2)//3)*4:raise ValueError('Invalid or oversized audio recording.')
        raw=base64.b64decode(encoded,validate=True)
        data,duration,billable=await asyncio.to_thread(normalize,raw)
    except (ValueError,subprocess.SubprocessError,EOFError,wave.Error):raise HTTPException(422,'Choose a valid WAV, MP3, M4A or OGG recording between 2 and 10 seconds, up to 8 MB.')
    asset=str(uuid.uuid4());key=object_key(owner,asset);stored=False
    try:
        client=media_storage._client();bucket=media_storage._storage_config()['bucket']
        await asyncio.to_thread(client.put_object,Bucket=bucket,Key=key,Body=data,ContentType='audio/wav',ContentLength=len(data),CacheControl='private, no-store')
        stored=True
        await database.execute('INSERT INTO speech_assets(id,user_id,object_key,duration_seconds,billable_seconds) VALUES (%s,%s,%s,%s,%s)',(asset,owner,key,duration,billable))
    except Exception:
        if stored:
            try:await asyncio.to_thread(client.delete_object,Bucket=bucket,Key=key)
            except Exception:pass
        raise HTTPException(503,'Private speech storage is temporarily unavailable.')
    return {'source_audio_id':asset,'private':True,'duration_seconds':duration,'billable_seconds':billable}
