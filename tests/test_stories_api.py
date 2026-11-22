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

from decimal import Decimal
from stories import StoryQuote, quote_story
from fastapi import HTTPException

@pytest.mark.asyncio
async def test_five_scene_total_and_no_lip_sync_claim():
    result = await quote_story(StoryQuote(scenes=[{'duration':6}]*5), 'owner')
    assert len(result['scenes']) == 5
    assert Decimal(result['total_credits']) == sum(Decimal(s['credits']) for s in result['scenes'])
    assert result['lip_sync_supported'] is False
    assert result['assembly_credits'] == '0'

@pytest.mark.asyncio
async def test_quote_uses_each_scene_duration_and_rejects_unsupported_provider():
    result = await quote_story(StoryQuote(scenes=[{'duration':6},{'duration':10,'resolution':'1080p'}]),'owner')
    assert Decimal(result['scenes'][1]['credits']) > Decimal(result['scenes'][0]['credits'])
    with pytest.raises(HTTPException):
        await quote_story(StoryQuote(scenes=[{'duration':6,'provider':'unsupported'}]),'owner')

