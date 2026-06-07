"""Tests for the orchestrator ABC + dataclasses (no Docker required)."""
from __future__ import annotations

import pytest

from orchestrator.base import (
    Orchestrator,
    PoolStatus,
    WorkerInfo,
    WorkerState,
)


class TestWorkerInfo:
    def test_to_dict_serialises_state_as_string(self):
        info = WorkerInfo(
            worker_id="w1", container_id="c1", pool="fast", queue="default",
            state=WorkerState.READY,
        )
        d = info.to_dict()
        assert d["state"] == "ready"
        assert d["worker_id"] == "w1"

    def test_state_defaults_to_starting(self):
        info = WorkerInfo(worker_id="w1", container_id="", pool="fast", queue="default")
        assert info.state == WorkerState.STARTING


class TestPoolStatus:
    def test_actual_size_excludes_dead_workers(self):
        status = PoolStatus(
            name="fast", queue="default", desired_size=3,
            workers=[
                WorkerInfo(worker_id="a", container_id="", pool="fast",
                           queue="default", state=WorkerState.READY),
                WorkerInfo(worker_id="b", container_id="", pool="fast",
                           queue="default", state=WorkerState.BUSY),
                WorkerInfo(worker_id="c", container_id="", pool="fast",
                           queue="default", state=WorkerState.DEAD),
            ],
        )
        assert status.actual_size == 2

    def test_to_dict_shape(self):
        status = PoolStatus(name="fast", queue="default,celery", desired_size=2)
        d = status.to_dict()
        assert d == {
            "name": "fast",
            "queue": "default,celery",
            "desired_size": 2,
            "actual_size": 0,
            "workers": [],
        }


class TestABCContract:
    def test_orchestrator_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError):
            Orchestrator()
