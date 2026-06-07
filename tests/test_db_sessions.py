"""Tests for the CRUD helpers in db/sessions.py + db/jobs.py."""
from __future__ import annotations

import uuid

import pytest


def test_create_session_generates_unique_short_code_and_secret(db_with_schema):
    from db.sessions import create_session

    a = create_session()
    b = create_session()
    assert a.short_code != b.short_code
    assert a.edit_secret != b.edit_secret
    # Detached after create — caller can use without a live session.
    assert a.id is not None


class TestShortCodeBidiSafety:
    """The CCv2 validator forbids ``__`` in component-key bases. Short
    codes flow into ``f"viewer_{short_code}"`` keys, so the generator
    must avoid producing codes that would introduce ``__`` at the
    concatenation site (leading/trailing ``_``), and a sanitiser is
    needed for legacy codes already in the DB."""

    def test_new_short_code_never_has_double_underscore(self):
        from db.sessions import new_short_code

        for _ in range(500):
            code = new_short_code()
            assert "__" not in code

    def test_new_short_code_never_starts_or_ends_with_underscore(self):
        """Defends against ``f"viewer_{code}"`` producing ``__``."""
        from db.sessions import new_short_code

        for _ in range(500):
            code = new_short_code()
            assert not code.startswith("_"), code
            assert not code.endswith("_"), code

    def test_safe_bidi_short_pads_leading_underscore(self):
        from db.sessions import safe_bidi_short

        # The exact session that triggered the bug:
        assert safe_bidi_short("_TlbKowr3xk") == "s_TlbKowr3xk"
        # Embedded in the v.s. failing key construction:
        assert "__" not in f"viewer_{safe_bidi_short('_TlbKowr3xk')}"

    def test_safe_bidi_short_pads_trailing_underscore(self):
        from db.sessions import safe_bidi_short

        assert safe_bidi_short("trailing_") == "trailing_s"

    def test_safe_bidi_short_collapses_double_underscore(self):
        from db.sessions import safe_bidi_short

        assert safe_bidi_short("abc__def") == "abc_def"
        # Triple+ collapses to single (idempotent loop).
        assert safe_bidi_short("abc___def") == "abc_def"

    def test_safe_bidi_short_is_no_op_for_clean_codes(self):
        from db.sessions import safe_bidi_short

        for code in ("abcDEFghi12", "x-Y_Z-12", "abc"):
            assert safe_bidi_short(code) == code

    def test_safe_bidi_short_preserves_uniqueness_at_boundaries(self):
        from db.sessions import safe_bidi_short

        # ``_abc`` and ``abc`` differ only in leading underscore;
        # the helper keeps them distinct rather than collapsing.
        assert safe_bidi_short("_abc") != safe_bidi_short("abc")
        assert safe_bidi_short("abc_") != safe_bidi_short("abc")
        assert safe_bidi_short("_abc") != safe_bidi_short("abc_")


def test_load_session_returns_row(db_with_schema):
    from db.sessions import create_session, load_session

    a = create_session(display_name="trial.xtc")
    loaded = load_session(a.short_code)
    assert loaded is not None
    assert loaded.id == a.id
    assert loaded.display_name == "trial.xtc"


def test_load_session_returns_none_for_unknown_code(db_with_schema):
    from db.sessions import load_session

    assert load_session("nope-doesnt-exist") is None


def test_is_editor_compares_constant_time(db_with_schema):
    from db.sessions import create_session, is_editor

    a = create_session()
    assert is_editor(a, a.edit_secret) is True
    assert is_editor(a, "wrong") is False
    assert is_editor(a, None) is False
    assert is_editor(a, "") is False


def test_mark_expired_idempotent(db_with_schema):
    from db.sessions import create_session, is_expired, mark_expired

    a = create_session()
    assert not is_expired(a)
    mark_expired(a)
    assert is_expired(a)
    # Calling twice doesn't crash + doesn't bump the timestamp.
    first_expiry = a.expired_at
    mark_expired(a)
    assert a.expired_at == first_expiry


def test_create_job_defaults_to_submitted(db_with_schema):
    from db.jobs import create_job
    from db.sessions import create_session

    s = create_session()
    job = create_job(session_id=s.id, kind="find_pockets")
    assert job.status == "submitted"
    assert job.kind == "find_pockets"
    assert job.session_id == s.id


def test_update_status_round_trip(db_with_schema):
    from db.jobs import create_job, get, update_status
    from db.sessions import create_session

    s = create_session()
    job = create_job(session_id=s.id, kind="cluster")

    update_status(
        job.id,
        "completed",
        step="Pocket clustering completed",
        result_info={"clusters_found": 5},
    )

    loaded = get(job.id)
    assert loaded is not None
    assert loaded.status == "completed"
    assert loaded.result_info == {"clusters_found": 5}


def test_update_status_unknown_job_raises(db_with_schema):
    from db.jobs import update_status

    with pytest.raises(LookupError):
        update_status(uuid.uuid4(), "running")


def test_find_by_session_filters_and_orders(db_with_schema):
    from db.jobs import create_job, find_by_session
    from db.sessions import create_session

    s = create_session()
    j1 = create_job(session_id=s.id, kind="find_pockets")
    j2 = create_job(session_id=s.id, kind="cluster")
    j3 = create_job(session_id=s.id, kind="docking")

    all_jobs = find_by_session(s.id)
    assert {j.id for j in all_jobs} == {j1.id, j2.id, j3.id}

    clustered = find_by_session(s.id, kind="cluster")
    assert [j.id for j in clustered] == [j2.id]
