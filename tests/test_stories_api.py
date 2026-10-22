"""Focused validation tests for durable Story API request contracts."""
import pytest
from pydantic import ValidationError
from stories import StoryCreate, SceneInput, ScenesSave

def test_story_rejects_unknown_owner_fields():
    with pytest.raises(ValidationError):
        StoryCreate(title="Story",prompt="A short film",user_id="someone-else")

def test_scene_duration_is_validated():
    with pytest.raises(ValidationError):
        SceneInput(scene_index=0,prompt="Opening scene",duration_seconds=7)

def test_scene_index_bounds_are_enforced():
    with pytest.raises(ValidationError):
        SceneInput(scene_index=50,prompt="Too many",duration_seconds=6)

def test_scenes_reject_empty_or_oversized_lists():
    with pytest.raises(ValidationError):
        ScenesSave(scenes=[])
    with pytest.raises(ValidationError):
        ScenesSave(scenes=[{"scene_index":i,"prompt":"Scene","duration_seconds":6} for i in range(51)])
