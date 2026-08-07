import pytest

from server.dispatcher import DispatchError, resolve_targets

NO_SESSIONS: dict[str, str] = {}


def test_bare_agent_name(registry):
    assert resolve_targets(["alpha"], registry, NO_SESSIONS).agents == ["alpha"]


def test_agent_prefix(registry):
    assert resolve_targets(["agent:beta"], registry, NO_SESSIONS).agents == ["beta"]


def test_bare_tag_matches_tag_list(registry):
    assert resolve_targets(["ops"], registry, NO_SESSIONS).agents == ["beta"]


def test_shared_tag_hits_both(registry):
    assert resolve_targets(["shared"], registry, NO_SESSIONS).agents == ["alpha", "beta"]


def test_global_fans_out(registry):
    assert resolve_targets(["global"], registry, NO_SESSIONS).agents == ["alpha", "beta"]


def test_overlapping_tags_are_deduplicated(registry):
    resolved = resolve_targets(["alpha", "shared", "research"], registry, NO_SESSIONS)
    assert resolved.agents == ["alpha", "beta"]


def test_session_tag_beats_everything(registry):
    sessions = {"beta": "sess-123"}
    resolved = resolve_targets(["session:sess-123", "global"], registry, sessions)
    assert resolved.agents == ["beta"]
    assert resolved.session_id == "sess-123"


def test_unknown_session_is_an_error_not_a_fallback(registry):
    with pytest.raises(DispatchError):
        resolve_targets(["session:nope", "global"], registry, {"beta": "sess-123"})


def test_no_match_raises_with_unmatched_tags(registry):
    with pytest.raises(DispatchError) as exc:
        resolve_targets(["nonsense"], registry, NO_SESSIONS)
    assert exc.value.unmatched == ["nonsense"]


def test_partial_match_succeeds_but_reports_unmatched(registry):
    resolved = resolve_targets(["alpha", "nonsense"], registry, NO_SESSIONS)
    assert resolved.agents == ["alpha"]
    assert resolved.unmatched == ["nonsense"]


def test_reserved_tags_rejected_in_agent_config(workdir):
    from pydantic import ValidationError

    from server.models import AgentConfig

    for bad in ("global", "session:x", "agent:x"):
        with pytest.raises(ValidationError):
            AgentConfig(name="x", tags=[bad], cwd=workdir)
