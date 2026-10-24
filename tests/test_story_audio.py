"""Exercise real FFmpeg output, rather than only checking command arguments."""
import json
import shutil
import subprocess
from array import array
from pathlib import Path
import pytest
import story_assembly_worker as worker
from stories import SceneInput

pytestmark=pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg not installed')

def run(*args):
    return subprocess.run(args,check=True,capture_output=True).stdout

def test_mixed_sound_and_silent_scene_assembly(tmp_path,monkeypatch):
    sources=[]
    for i,freq in enumerate([440,None,880]):
        path=tmp_path/f'{i}.mp4'
        args=['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i',f'color=c=black:s=160x90:r=24:d=1']
        if freq:args+=['-f','lavfi','-i',f'sine=frequency={freq}:sample_rate=44100:duration={1.6 if i==2 else 0.6}','-c:a','aac']
        run(*args,'-c:v','libx264','-pix_fmt','yuv420p',str(path))
        sources.append(path)
    def download(key,destination):
        shutil.copyfile(sources[int(key)],destination)
        return Path(destination).stat().st_size
    output=tmp_path/'assembled.mp4'
    monkeypatch.setattr(worker,'_download_private_object',download)
    def upload(path,job):
        shutil.copyfile(path,output)
        return {'object_key':'private/result.mp4'}
    monkeypatch.setattr(worker.media_storage,'upload_video_file',upload)
    worker._assemble_and_upload(['0','1','2'],'test')
    data=json.loads(run('ffprobe','-v','error','-show_streams','-show_format','-of','json',str(output)))
    audio=next(s for s in data['streams'] if s['codec_type']=='audio')
    assert audio['sample_rate']=='48000' and audio['channels']==2
    assert 2.9<float(data['format']['duration'])<3.2
    samples=array('f',run('ffmpeg','-v','error','-i',str(output),'-vn','-ac','1','-ar','48000','-f','f32le','pipe:1'))
    def segment(start,end):return samples[int(start*48000):int(end*48000)]
    def rms(xs):return (sum(x*x for x in xs)/len(xs))**0.5
    assert rms(segment(.15,.45))>.03
    assert rms(segment(1.15,1.8))<.002
    assert rms(segment(2.15,2.45))>.03
    # Short soundtrack padding does not cut the video or shift later scene sound.
    assert rms(segment(.8,.95))<.002
    def crossings(xs):return sum(a<=0<b for a,b in zip(xs,xs[1:]))
    assert 100<crossings(segment(.15,.45))<160
    assert 230<crossings(segment(2.15,2.45))<300
    muted=tmp_path/'muted.mp4'
    worker._normalize_scene(sources[0],muted,preserve_audio=False)
    silence=array('f',run('ffmpeg','-v','error','-i',str(muted),'-vn','-ac','1','-ar','48000','-f','f32le','pipe:1'))
    assert rms(silence)<.002

def test_scene_sound_settings_are_strict_and_bounded():
    from pydantic import ValidationError
    assert SceneInput(scene_index=0,prompt='Opening').generate_audio is True
    assert SceneInput(scene_index=0,prompt='Opening',generate_audio=False).generate_audio is False
    with pytest.raises(ValidationError):SceneInput(scene_index=0,prompt='Opening',generate_audio='false')
    with pytest.raises(ValidationError):SceneInput(scene_index=0,prompt='Opening',sound_direction='x'*501)
