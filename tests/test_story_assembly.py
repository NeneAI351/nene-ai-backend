"""Unit tests for durable Story assembly guards (no live storage or provider calls)."""
import pytest
import story_assembly_worker
from stories import StoryCreate, SceneInput, ScenesSave, _ensure_unique_scene_indexes
from pydantic import ValidationError

@pytest.mark.parametrize("key",[
    "https://attacker.example/video.mp4",
    "generations/not-a-uuid/video.mp4",
    "../generations/00000000-0000-0000-0000-000000000000/video.mp4",
    "generations/00000000-0000-0000-0000-000000000000/private.mp4",
])
def test_assembly_rejects_invalid_private_object_key_before_storage(key):
    with pytest.raises(ValueError):
        story_assembly_worker._download_private_object(key,"/tmp/never-written.mp4")

def test_story_create_rejects_client_supplied_owner():
    with pytest.raises(ValidationError):
        StoryCreate(title="A story",prompt="A prompt",user_id="other-user")

@pytest.mark.parametrize("duration",[6,8,10])
def test_supported_scene_durations(duration):
    assert SceneInput(scene_index=0,prompt="Opening",duration_seconds=duration).duration_seconds==duration

@pytest.mark.parametrize("duration",[0,1,3,7,11])
def test_unsupported_scene_durations_are_rejected(duration):
    with pytest.raises(ValidationError):
        SceneInput(scene_index=0,prompt="Opening",duration_seconds=duration)

def test_duplicate_scene_indexes_are_rejected():
    with pytest.raises(ValueError,match="unique"):
        _ensure_unique_scene_indexes([
            {"scene_index":0,"prompt":"Scene one","duration_seconds":6},
            {"scene_index":0,"prompt":"Duplicate","duration_seconds":6},
        ])

def test_scene_count_contract_caps_at_fifty():
    with pytest.raises(ValidationError):
        ScenesSave(scenes=[{"scene_index":i,"prompt":"Scene","duration_seconds":6} for i in range(51)])
