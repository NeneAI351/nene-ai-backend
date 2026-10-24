"""Fast unit tests for generation lifecycle invariants that need no database."""
import pytest

import generation_store


def test_terminal_states_are_immutable():
    assert generation_store.ALLOWED_TRANSITIONS["succeeded"] == set()
    assert generation_store.ALLOWED_TRANSITIONS["failed"] == set()


def test_unknown_submission_must_reconcile_not_resubmit():
    assert "submitting" not in generation_store.ALLOWED_TRANSITIONS["submission_unknown"]
    assert generation_store.ALLOWED_TRANSITIONS["submission_unknown"] == {
        "submitted", "running", "succeeded", "failed"
    }


def test_snapshot_strips_secrets_recursively_and_preserves_safe_fields():
    snapshot = generation_store._safe_snapshot({
        "prompt": "A cinematic beach",
        "provider": "ltx",
        "api_key": "must-not-persist",
        "nested": {"Authorization": "Bearer secret", "duration": 6},
    })
    assert "must-not-persist" not in snapshot
    assert "Bearer secret" not in snapshot
    assert '"prompt":"A cinematic beach"' in snapshot
    assert '"duration":6' in snapshot


def test_snapshot_rejects_oversized_payload():
    with pytest.raises(ValueError, match="too large"):
        generation_store._safe_snapshot({"prompt": "x" * (generation_store.MAX_REQUEST_BYTES + 1)})


def test_snapshot_rejects_non_json_values():
    with pytest.raises(ValueError, match="unsupported value"):
        generation_store._safe_snapshot({"bad": object()})


def test_state_transition_rejects_unknown_status():
    with pytest.raises(generation_store.GenerationConflict, match="Unknown generation status"):
        # This validation occurs before the database pool is touched.
        import asyncio
        asyncio.run(generation_store.transition_generation("not-a-uuid", "not-a-uuid", "provider-done"))
