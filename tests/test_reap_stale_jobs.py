"""Tests for ``cleanup_job.reap_stale_jobs_task``.

Behaviour under test:
* A Job row older than its kind's ``STALE_JOB_CUTOFF_SECONDS``, still in an
  in-flight status ('submitted' / 'queued' / 'running'), is marked 'failed'
  with a structured error dict (``exc_type`` / ``exc_message`` / ``stage``).
* A Job row inside its cutoff is left untouched.
* An already-terminal Job row (e.g. 'completed', 'failed') is never
  touched, no matter how old — the reaper only looks at in-flight rows.
* Per-kind cutoffs are respected: a docking job isn't reaped on the much
  shorter find_pockets/cluster timescale, only on its own (longer) one.
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone


def _seed_job(db, *, kind, status="running", age_hours, suffix=""):
    """Insert a Session + Job row whose ``updated_at`` is backdated to
    ``age_hours`` ago — simulating a job that last transitioned that long
    ago and has heard nothing from a worker since."""
    from db.models import Job, Session

    s = Session(
        short_code=f"rs-{_uuid.uuid4().hex[:10]}",
        edit_secret="x" * 32,
        state={},
    )
    db.add(s)
    db.flush()

    j = Job(
        session_id=s.id,
        kind=kind,
        status=status,
        legacy_id=f"{kind}-{suffix or _uuid.uuid4().hex[:8]}",
    )
    db.add(j)
    db.flush()

    stuck_since = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    j.updated_at = stuck_since.replace(tzinfo=None)
    db.flush()
    db.commit()
    return j


class TestReapStaleJobs:
    def test_reaps_job_past_cutoff(self, db_with_schema):
        from cleanup_job import STALE_JOB_CUTOFF_SECONDS, reap_stale_jobs_task
        from db.jobs import get as get_job
        from db.session import get_db

        cutoff_hours = STALE_JOB_CUTOFF_SECONDS["find_pockets"] / 3600
        with get_db() as db:
            j = _seed_job(db, kind="find_pockets", status="running",
                          age_hours=cutoff_hours + 1)
            jid = j.id

        result = reap_stale_jobs_task()
        assert result["status"] == "success"
        assert result["reaped"] == 1

        row = get_job(jid)
        assert row.status == "failed"
        assert row.error["exc_type"] == "StaleJobReaped"
        assert "find_pockets" in row.error["exc_message"]
        assert row.error["stage"] == "reaper"

    def test_does_not_reap_job_inside_cutoff(self, db_with_schema):
        from cleanup_job import STALE_JOB_CUTOFF_SECONDS, reap_stale_jobs_task
        from db.jobs import get as get_job
        from db.session import get_db

        cutoff_hours = STALE_JOB_CUTOFF_SECONDS["find_pockets"] / 3600
        with get_db() as db:
            j = _seed_job(db, kind="find_pockets", status="running",
                          age_hours=max(cutoff_hours - 1, 0.1))
            jid = j.id

        result = reap_stale_jobs_task()
        assert result["reaped"] == 0

        row = get_job(jid)
        assert row.status == "running"
        assert row.error is None

    def test_does_not_touch_terminal_job(self, db_with_schema):
        from cleanup_job import STALE_JOB_CUTOFF_SECONDS, reap_stale_jobs_task
        from db.jobs import get as get_job
        from db.session import get_db

        cutoff_hours = STALE_JOB_CUTOFF_SECONDS["find_pockets"] / 3600
        with get_db() as db:
            j = _seed_job(db, kind="find_pockets", status="failed",
                          age_hours=cutoff_hours * 10)
            jid = j.id
            j.error = {"exc_type": "ValueError", "exc_message": "original failure"}
            db.flush()

        result = reap_stale_jobs_task()
        assert result["reaped"] == 0

        row = get_job(jid)
        assert row.status == "failed"
        # Original error preserved, not clobbered by the reaper.
        assert row.error["exc_type"] == "ValueError"

    def test_docking_job_not_reaped_on_find_pockets_timescale(self, db_with_schema):
        """A docking job stuck longer than find_pockets' cutoff but still
        inside docking's own (much longer) cutoff must survive."""
        from cleanup_job import STALE_JOB_CUTOFF_SECONDS, reap_stale_jobs_task
        from db.jobs import get as get_job
        from db.session import get_db

        fp_cutoff_hours = STALE_JOB_CUTOFF_SECONDS["find_pockets"] / 3600
        docking_cutoff_hours = STALE_JOB_CUTOFF_SECONDS["docking"] / 3600
        assert docking_cutoff_hours > fp_cutoff_hours  # sanity: docking IS longer

        stuck_hours = fp_cutoff_hours + 0.5  # past find_pockets', inside docking's
        assert stuck_hours < docking_cutoff_hours

        with get_db() as db:
            j = _seed_job(db, kind="docking", status="running", age_hours=stuck_hours)
            jid = j.id

        result = reap_stale_jobs_task()
        assert result["reaped"] == 0

        row = get_job(jid)
        assert row.status == "running"

    def test_docking_job_reaped_past_its_own_cutoff(self, db_with_schema):
        from cleanup_job import STALE_JOB_CUTOFF_SECONDS, reap_stale_jobs_task
        from db.jobs import get as get_job
        from db.session import get_db

        docking_cutoff_hours = STALE_JOB_CUTOFF_SECONDS["docking"] / 3600
        with get_db() as db:
            j = _seed_job(db, kind="docking", status="submitted",
                          age_hours=docking_cutoff_hours + 1)
            jid = j.id

        result = reap_stale_jobs_task()
        assert result["reaped"] == 1

        row = get_job(jid)
        assert row.status == "failed"
        assert "docking" in row.error["exc_message"]

    def test_cluster_job_reaped_past_its_own_cutoff(self, db_with_schema):
        from cleanup_job import STALE_JOB_CUTOFF_SECONDS, reap_stale_jobs_task
        from db.jobs import get as get_job
        from db.session import get_db

        cluster_cutoff_hours = STALE_JOB_CUTOFF_SECONDS["cluster"] / 3600
        with get_db() as db:
            j = _seed_job(db, kind="cluster", status="queued",
                          age_hours=cluster_cutoff_hours + 1)
            jid = j.id

        result = reap_stale_jobs_task()
        assert result["reaped"] == 1

        row = get_job(jid)
        assert row.status == "failed"

    def test_mixed_population(self, db_with_schema):
        """Several rows at once — only the ones past THEIR OWN kind's
        cutoff get reaped; everything else (inside-cutoff, longer-lived
        kinds, and terminal rows) survives."""
        from cleanup_job import STALE_JOB_CUTOFF_SECONDS, reap_stale_jobs_task
        from db.session import get_db

        fp = STALE_JOB_CUTOFF_SECONDS["find_pockets"] / 3600
        cl = STALE_JOB_CUTOFF_SECONDS["cluster"] / 3600
        dk = STALE_JOB_CUTOFF_SECONDS["docking"] / 3600

        with get_db() as db:
            # Should be reaped — past their own cutoff.
            _seed_job(db, kind="find_pockets", status="running",
                      age_hours=fp + 1, suffix="a")
            _seed_job(db, kind="cluster", status="submitted",
                      age_hours=cl + 1, suffix="b")
            _seed_job(db, kind="docking", status="running",
                      age_hours=dk + 1, suffix="c")
            # Should survive — inside their own cutoff.
            _seed_job(db, kind="find_pockets", status="running",
                      age_hours=0.1, suffix="d")
            _seed_job(db, kind="cluster", status="queued",
                      age_hours=cl - 0.5, suffix="e")
            # Should survive — docking, past find_pockets' timescale but
            # inside docking's own much longer cutoff.
            _seed_job(db, kind="docking", status="running",
                      age_hours=fp + 0.1, suffix="f")
            # Should survive — terminal, however old.
            _seed_job(db, kind="find_pockets", status="completed",
                      age_hours=dk * 5, suffix="g")

        result = reap_stale_jobs_task()
        assert result["reaped"] == 3
