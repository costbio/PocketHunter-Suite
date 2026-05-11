"""Custom exception classes raised by the Celery tasks on structured failure.

Keeping these in a tiny module lets the failure_view classifier check
``exc_type`` against known names without importing the full ``tasks`` module
(which would pull in Celery + ProDy at classifier import time).
"""
from __future__ import annotations


class DetectionProducedNoOutput(Exception):
    """p2rank ran but produced no usable ``pockets.csv``.

    Almost always means p2rank crashed (Java OOM, missing JRE, malformed
    input PDB), but its stdout/stderr was suppressed by the underlying CLI.
    """


class ClusteringFoundNoClusters(Exception):
    """The clustering subprocess succeeded but yielded zero clusters.

    DBSCAN with the supplied ``min_prob`` threshold found no dense regions,
    or hierarchical clustering produced no representatives. The user fix is
    usually to lower ``min_prob``, lower the trajectory stride, or switch
    clustering method.
    """


class DockingProducedNoResults(Exception):
    """smina ran but every receptor/ligand pair failed or yielded no poses."""
