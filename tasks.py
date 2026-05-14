import subprocess
import os
import re
import uuid
import shutil
import json
from celery_app import celery_app
from celery.exceptions import SoftTimeLimitExceeded
import time
from datetime import datetime
import pandas as pd
from config import Config
from logging_config import setup_logging

# Use Config for all paths
POCKETHUNTER_DIR = str(Config.POCKETHUNTER_DIR)
POCKETHUNTER_CLI = str(Config.POCKETHUNTER_CLI)
RESULTS_DIR = str(Config.RESULTS_DIR)

# Process timeout constants (in seconds)
PIPELINE_TIMEOUT = 3600  # 1 hour for full pipeline
EXTRACT_TIMEOUT = 1800   # 30 minutes for frame extraction
DETECT_TIMEOUT = 3600    # 1 hour for pocket detection
CLUSTER_TIMEOUT = 1800   # 30 minutes for clustering

# Setup logging
logger = setup_logging(__name__)


def _update_status_file(job_id, status, step=None, task_id=None, result_info=None,
                        prefix='', error=None):
    """Update the job status JSON file on disk + mirror into the Job row.

    On failure, pass an ``error`` dict containing at minimum ``exc_type``,
    ``exc_message``, and ``stage`` so the UI can render a structured failure
    panel without consulting the Celery result backend (which expires).

    v2 Phase A3: after the disk write, the same fields are mirrored into
    ``db.Job`` via ``update_by_legacy_id(job_id, …)``. The DB write is a
    silent no-op when no Job row was registered at submission time
    (legacy v1 callers, or pages that submit without a loaded session).
    Phase C removes the disk-file leg of this; for now both write paths
    coexist so the UI keeps working unchanged.
    """
    try:
        filename = f'{prefix}{job_id}_status.json' if prefix else f'{job_id}_status.json'
        status_file = os.path.join(RESULTS_DIR, filename)
        current_status = {}
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                try:
                    current_status = json.load(f)
                except json.JSONDecodeError:
                    current_status = {}
        current_status['status'] = status
        if step:
            current_status['step'] = step
        if task_id:
            current_status['task_id'] = task_id
        if result_info:
            current_status['result_info'] = result_info
        if error is not None:
            current_status['error'] = error
        current_status['last_updated'] = datetime.now().isoformat()
        with open(status_file, 'w') as f:
            json.dump(current_status, f, indent=4)
        logger.info(f"Status file updated: {status_file} -> {status}")
    except Exception as e:
        logger.warning(f"Failed to update status file for {job_id}: {e}")

    # Mirror to DB. Best-effort; never blocks the on-disk write or the task.
    try:
        from db.jobs import update_by_legacy_id
        update_by_legacy_id(
            job_id,
            status,
            step=step,
            celery_task_id=task_id,
            result_info=result_info,
            error=error,
        )
    except Exception as db_err:
        # The session-bound Job row is opportunistic; never raise to the caller.
        logger.debug(f"DB mirror for {job_id} skipped: {db_err}")


# Public re-export — pages import this directly rather than reimplementing it.
update_status_file = _update_status_file


def _fail_job(celery_task, job_id, stage, exc, log_path=None):
    """Single chokepoint for marking a Celery task FAILED on disk + backend.

    Writes a structured ``error`` dict into ``<job>_status.json`` and emits
    the matching Celery FAILURE meta. Returns nothing; caller still re-raises.
    """
    err = {
        'exc_type': type(exc).__name__,
        'exc_message': str(exc),
        'stage': stage,
    }
    if log_path:
        err['log_path'] = log_path
    _update_status_file(job_id, 'failed', f'{stage} failed: {exc}',
                        task_id=celery_task.request.id, error=err)
    celery_task.update_state(state='FAILURE', meta={
        'status': f'{stage} failed: {exc}',
        'exc_type': err['exc_type'],
        'exc_message': err['exc_message'],
        'stage': stage,
        **({'log_path': log_path} if log_path else {}),
    })


def _write_viewer_file(job_id: str, pdb_dir: str) -> dict:
    """Generate the per-job ``viewer.cif`` (v2 Phase B B2).

    Reads every ``.pdb`` in ``pdb_dir`` and writes a multi-model mmCIF
    suitable for the Mol* viewer. Returns a dict of result_info fields
    the caller merges into its overall ``results_overview`` — so the Job
    row picks them up via the existing ``_update_status_file`` mirror.

    On success the dict contains ``viewer_file_path`` + ``viewer_file_format``.
    On size-cap exceeded, ``viewer_file_warning``.
    On any other failure, ``viewer_file_error``.

    Never raises. Analysis tasks complete on their own merits regardless
    of whether the viewer artefact was producible.
    """
    try:
        from viewer_pipeline import (
            MAX_VIEWER_BYTES,
            VIEWER_FILE_FORMAT,
            VIEWER_FILE_NAME,
            convert_pdb_dir_to_viewer,
            estimate_viewer_size,
        )
    except Exception as e:  # gemmi import failed, module-load failed, etc.
        logger.warning(f"viewer_pipeline import failed for {job_id}: {e}")
        return {"viewer_file_error": f"viewer_pipeline unavailable: {e}"}

    try:
        from pathlib import Path
        pdb_dir_path = Path(pdb_dir)
        out_path = Path(RESULTS_DIR) / job_id / VIEWER_FILE_NAME

        est = estimate_viewer_size(pdb_dir_path)
        if est > MAX_VIEWER_BYTES:
            msg = (
                f"Estimated viewer file ~{est / (1024**2):.0f} MB exceeds "
                f"limit {MAX_VIEWER_BYTES / (1024**2):.0f} MB; skipped."
            )
            logger.warning(f"viewer skipped for {job_id}: {msg}")
            return {"viewer_file_warning": msg}

        convert_pdb_dir_to_viewer(pdb_dir_path, out_path)
        return {
            "viewer_file_path": str(out_path),
            "viewer_file_format": VIEWER_FILE_FORMAT,
        }
    except Exception as e:
        logger.warning(f"viewer file generation failed for {job_id}: {e}")
        return {"viewer_file_error": str(e)}


def _write_pair_failure_log(job_id, pair_failures, pairs_total):
    """Write a plain-text per-pair failure log next to results/<job>/error.log.

    Mirrors the format of ``_run_stage``'s stage-level error log so the UI
    can offer it as a single download. Returns the absolute path on success
    or ``None`` if I/O fails (the task itself should not crash on log write).
    """
    if not pair_failures:
        return None
    try:
        job_dir = os.path.join(RESULTS_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        log_path = os.path.join(job_dir, 'docking_pair_failures.log')
        with open(log_path, 'w', errors='replace') as f:
            f.write(
                f"=== Docking pair failures for job {job_id} "
                f"({len(pair_failures)} pairs failed of {pairs_total}) ===\n\n"
            )
            for i, rec in enumerate(pair_failures, 1):
                f.write(f"[{i}] receptor={rec.get('receptor', '?')} · ligand={rec.get('ligand', '?')}\n")
                f.write(f"    exc_type: {rec.get('exc_type', 'Unknown')}\n")
                f.write(f"    exc_message: {rec.get('exc_message', '')}\n")
                tail = rec.get('stderr_tail') or ''
                if tail:
                    f.write("    stderr (tail):\n")
                    for line in tail.splitlines():
                        f.write(f"        {line}\n")
                f.write("\n")
        return log_path
    except OSError as e:
        logger.warning(f"Could not write docking_pair_failures.log for {job_id}: {e}")
        return None


def _run_stage(celery_task, command, cwd, timeout, prog_start, prog_end, stage_name,
               update_interval=2, job_id=None):
    """
    Run a subprocess stage and poll it, emitting Celery progress updates within [prog_start, prog_end].

    stdout/stderr are redirected to files to avoid OS pipe-buffer deadlocks that
    occur when a subprocess writes more than ~64 KB without being drained.

    **Log paths (v2 B8):** when ``job_id`` is supplied, logs go to a stable
    location under ``results/<job_id>/.live/<sanitized_stage>.{stdout,stderr}.log``
    and are *kept* after the stage completes — the live-log fragment in
    ``analysis_app.py`` tails them in real time, and they stay around for
    post-hoc diagnosis (pruned by ``cleanup_job`` along with the rest of
    the job dir). When ``job_id`` is ``None`` (unit tests), the helper
    falls back to ``tempfile.NamedTemporaryFile`` paths that get deleted
    on return.

    On non-zero exit *and* when ``job_id`` is supplied, the captured stderr is also
    written to ``results/<job_id>/error.log`` so the UI can offer it as a download.

    Returns (stdout_text, stderr_text) on success; raises Exception on failure or timeout.
    """
    import tempfile

    start_time = time.time()
    prog_range = prog_end - prog_start

    # Stable per-job log paths so the UI can tail them while the stage runs.
    # Fall back to deletable temp files for tests / non-job callers.
    delete_on_exit = False
    if job_id:
        try:
            from live_log import sanitize_stage_name as _sanitize
        except Exception:
            _sanitize = lambda s: re.sub(r'[^A-Za-z0-9_-]+', '_', s).strip('_').lower() or 'stage'
        live_dir = os.path.join(RESULTS_DIR, job_id, '.live')
        os.makedirs(live_dir, exist_ok=True)
        stem = _sanitize(stage_name)
        stdout_path = os.path.join(live_dir, f'{stem}.stdout.log')
        stderr_path = os.path.join(live_dir, f'{stem}.stderr.log')
        # Truncate any previous run's log under the same job_id + stage.
        open(stdout_path, 'w').close()
        open(stderr_path, 'w').close()
    else:
        fout = tempfile.NamedTemporaryFile(mode='w', suffix='_stdout.txt', delete=False)
        ferr = tempfile.NamedTemporaryFile(mode='w', suffix='_stderr.txt', delete=False)
        stdout_path = fout.name
        stderr_path = ferr.name
        fout.close()
        ferr.close()
        delete_on_exit = True

    try:
        with open(stdout_path, 'w') as fout_h, open(stderr_path, 'w') as ferr_h:
            process = subprocess.Popen(command, stdout=fout_h, stderr=ferr_h, cwd=cwd)

        last_update = start_time
        while process.poll() is None:
            time.sleep(1)
            now = time.time()
            elapsed = now - start_time

            if elapsed > timeout:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(command, timeout)

            if now - last_update >= update_interval:
                # Logarithmic ramp: fast early, slow late — never quite reaches prog_end
                frac = min(0.92, 1 - 1 / (1 + elapsed / (timeout * 0.2)))
                progress = int(prog_start + frac * prog_range)
                celery_task.update_state(
                    state='PROGRESS',
                    meta={
                        'current_step': f'{stage_name} ({int(elapsed)}s elapsed)',
                        'progress': progress,
                        'stage': stage_name,
                        'elapsed': elapsed,
                    }
                )
                last_update = now

        process.wait()  # ensure returncode is fully set

        with open(stdout_path, 'r', errors='replace') as f:
            stdout = f.read()
        with open(stderr_path, 'r', errors='replace') as f:
            stderr = f.read()

        if process.returncode != 0:
            # Persist the full stderr + stdout tail to the job folder so the UI
            # can offer it as a download. Best-effort: if it fails, fall back to
            # the inline truncated stderr.
            if job_id:
                try:
                    job_dir = os.path.join(RESULTS_DIR, job_id)
                    os.makedirs(job_dir, exist_ok=True)
                    error_log = os.path.join(job_dir, 'error.log')
                    with open(error_log, 'w', errors='replace') as f:
                        f.write(f"=== {stage_name} (exit {process.returncode}) ===\n\n")
                        f.write("--- STDOUT (tail 4000 chars) ---\n")
                        f.write(stdout[-4000:])
                        f.write("\n\n--- STDERR (full) ---\n")
                        f.write(stderr)
                except OSError as log_err:
                    logger.warning(f"Could not write error.log for {job_id}: {log_err}")
            raise Exception(
                f"{stage_name} failed (exit {process.returncode}). Stderr: {stderr[-2000:]}"
            )
        return stdout, stderr
    finally:
        if delete_on_exit:
            for p in (stdout_path, stderr_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _count_sdf_molecules(path: str) -> int:
    """Cheap molecule count for an SDF — counts the ``$$$$`` separator.

    Returns 1 for any other format or on read error. Used to drive the
    docking task's pre-stage progress bar without parsing the SDF.
    """
    suffix = os.path.splitext(path)[1].lower()
    if suffix != ".sdf":
        return 1
    try:
        with open(path, "r", errors="replace") as f:
            count = sum(1 for line in f if line.strip() == "$$$$")
        return max(1, count)
    except OSError:
        return 1


_SDF_TAG_RE = re.compile(r">\s*<([^>]+)>")


def _sdf_molecule_names(path: str) -> list[str]:
    """Per-record display name for an SDF, in record order (B11.21).

    Prefers a ``> <name>`` data field (tag matched case-insensitively),
    falls back to the molecule title line (record line 1 — which is
    legitimately blank in e-Drug3D SDFs), then to ``""`` (callers
    substitute the PDBQT stem). Non-SDF inputs → ``[""]``.

    Records are split on ``$$$$`` *lines* (not the substring) so a
    record's first line is always its title — no leading-newline
    artifact to strip.
    """
    if not path.lower().endswith(".sdf"):
        return [""]
    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return [""]

    def _name_of(record: list[str]) -> str:
        title = record[0].strip() if record else ""
        for i, ln in enumerate(record):
            m = _SDF_TAG_RE.match(ln.strip())
            if m and m.group(1).strip().lower() == "name":
                if i + 1 < len(record):
                    return record[i + 1].strip() or title
                break
        return title

    names: list[str] = []
    record: list[str] = []
    for ln in lines:
        if ln.strip() == "$$$$":
            names.append(_name_of(record))
            record = []
        else:
            record.append(ln)
    # Tolerate a trailing record with no closing ``$$$$``.
    if any(l.strip() for l in record):
        names.append(_name_of(record))
    return names or [""]


def _prepare_ligands_with_progress(
    celery_task,
    ligand_folder: str,
    *,
    gen_3d: bool = False,
    progress_low: int = 2,
    progress_high: int = 15,
) -> tuple[list[dict], dict[str, str]]:
    """Convert SDF/PDB ligand inputs to PDBQT, posting progress to the task.

    Reads every non-PDBQT file in ``ligand_folder``, runs obabel with
    ``-m`` (one PDBQT per molecule, what smina wants), and deletes the
    source file on *full* success. Pre-counts molecules via
    ``_count_sdf_molecules`` so progress reflects "N of M molecules
    prepared". When ``gen_3d`` is true, passes ``--gen3d`` to obabel
    (slower, but needed for 2D inputs).

    When obabel converts *fewer* PDBQT than the input held (a malformed
    SDF record, an obabel error), the shortfall is **recorded, not
    raised** — the docking run proceeds with whatever converted and the
    results view surfaces a callout.

    Returns ``(conversion_failures, ligand_names)``:
      * ``conversion_failures`` — list of ``{source_file, expected,
        converted, error}`` records, empty on a clean conversion.
      * ``ligand_names`` — ``{pdbqt_stem: display_name}`` map (B11.21),
        also written to ``ligand_names.json`` in ``ligand_folder``, so
        the docking grid can show real molecule names instead of
        ``<stem>_<N>`` PDBQT stems.

    Progress maps into ``[progress_low, progress_high]`` so the
    caller's overall percentage stays consistent across the
    prep + dock stages.
    """
    import glob as _glob
    folder = ligand_folder
    inputs = sorted(
        p for p in _glob.glob(os.path.join(folder, "*"))
        if p.lower().endswith((".sdf", ".pdb"))
    )
    if not inputs:
        # Nothing to convert. Either everything was already PDBQT or
        # the upload was empty (caller surfaces "no files" separately).
        return [], {}

    total_molecules = sum(_count_sdf_molecules(p) for p in inputs)
    span = max(0, progress_high - progress_low)

    def _report(converted: int, current_file: str | None = None) -> None:
        pct = progress_low + (
            int(span * converted / total_molecules) if total_molecules else span
        )
        msg = f"Preparing ligands: {converted} / {total_molecules} molecules"
        if current_file:
            msg += f" — {os.path.basename(current_file)}"
        celery_task.update_state(state="PROGRESS", meta={
            "current_step": msg,
            "progress": min(pct, progress_high),
        })

    _report(0)

    converted = 0
    conversion_failures: list[dict] = []
    ligand_names: dict[str, str] = {}
    # ``-m`` splits multi-molecule input into one PDBQT per molecule
    # (named ``<stem>_1.pdbqt``, ``<stem>_2.pdbqt``, …). smina docks
    # one molecule per file; the old ``--separate`` flag actually just
    # concatenates into a single multi-MODEL file that smina rejects
    # with "Use vina_split first".
    obabel_args = ["-m"]
    if gen_3d:
        obabel_args.append("--gen3d")

    for path in inputs:
        n_mols = _count_sdf_molecules(path)
        stem = os.path.splitext(path)[0]
        out_template = stem + "_.pdbqt"
        cmd = ["obabel", path, "-O", out_template] + obabel_args

        # Run obabel as a subprocess. Poll the output dir between
        # checks so the progress bar moves while obabel is grinding.
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        while proc.poll() is None:
            try:
                produced_so_far = len(_glob.glob(stem + "_*.pdbqt"))
            except OSError:
                produced_so_far = 0
            _report(converted + min(produced_so_far, n_mols), path)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                continue
        stderr = b""
        if proc.stderr is not None:
            try:
                stderr = proc.stderr.read()
            except Exception:
                stderr = b""

        produced = sorted(_glob.glob(stem + "_*.pdbqt"))
        if not produced and os.path.exists(stem + ".pdbqt"):
            produced = [stem + ".pdbqt"]
        n_produced = len(produced)
        converted += n_produced
        _report(converted, path)

        # B11.21: map each produced PDBQT to its source molecule's name.
        # obabel -m numbers files ``<stem>_<N>.pdbqt`` in SDF-record
        # order, so the trailing _<N> is the 1-based record index.
        mol_names = _sdf_molecule_names(path)
        for pdbqt_path in produced:
            base = os.path.basename(pdbqt_path)
            stem_key = base[:-6] if base.lower().endswith(".pdbqt") else base
            m = re.search(r"_(\d+)\.pdbqt$", base)
            if m:
                idx = int(m.group(1)) - 1
                disp = mol_names[idx] if 0 <= idx < len(mol_names) else ""
            else:
                disp = mol_names[0] if mol_names else ""
            ligand_names[stem_key] = disp or stem_key

        if n_produced < n_mols:
            # Under-conversion — a malformed SDF record or an obabel
            # error. Record it and carry on; the docking run still docks
            # whatever converted (see render_ligand_conversion_callout).
            if proc.returncode != 0:
                err = ((stderr.decode("utf-8", "replace") or "").strip()[:300]
                       or "obabel exited with a non-zero status")
            else:
                err = ("obabel produced fewer PDBQT files than molecules "
                       "in the input — the file likely has malformed records")
            conversion_failures.append({
                "source_file": os.path.basename(path),
                "expected": n_mols,
                "converted": n_produced,
                "error": err,
            })
            # Leave the source file in place so it stays inspectable.
        else:
            # Full success — drop the source so the dir holds only PDBQT.
            try:
                os.remove(path)
            except OSError:
                pass

    # B11.21: persist the name map next to the PDBQTs (debug aid +
    # robustness) and return it so run_docking_task can label the CSV.
    if ligand_names:
        try:
            with open(os.path.join(folder, "ligand_names.json"), "w") as fh:
                json.dump(ligand_names, fh)
        except OSError:
            pass

    return conversion_failures, ligand_names


@celery_app.task(bind=True)
def run_pockethunter_pipeline(self, xtc_file_path, topology_file_path, job_id, stride=10, num_threads=None,
                               min_prob=0.5, clustering_method='dbscan', run_docking=False,
                               ligand_folder=None, num_poses=10, exhaustiveness=8, ph_value=7.4,
                               box_size_x=20.0, box_size_y=20.0, box_size_z=20.0):
    """
    PocketHunter full pipeline: extract → detect → cluster → (optional) dock.
    Each stage reports real progress via Celery state updates.

    Progress ranges:
      0  –  25%  Extract frames to PDB
      25 –  60%  Detect pockets (p2rank)
      60 –  80%  Cluster pockets
      80 –  97%  Molecular docking (optional)
    """
    from task_errors import ClusteringFoundNoClusters, DetectionProducedNoOutput

    # B11.21: p2rank thread count is .env-driven (Config.P2RANK_THREADS),
    # not user-facing. ``num_threads`` is kept as an optional override.
    if num_threads is None:
        num_threads = Config.P2RANK_THREADS

    pipeline_start = time.time()
    output_folder_job = os.path.join(RESULTS_DIR, job_id)
    os.makedirs(output_folder_job, exist_ok=True)
    error_log_path = os.path.join(output_folder_job, 'error.log')

    # ── Stage 1: Extract frames ──────────────────────────────────────────
    output_pdb_dir = os.path.join(output_folder_job, 'pdbs')
    os.makedirs(output_pdb_dir, exist_ok=True)

    self.update_state(state='PROGRESS', meta={
        'current_step': 'Extracting frames from trajectory…',
        'progress': 0,
        'stage': 'extract',
    })

    cmd_extract = [
        'python', POCKETHUNTER_CLI, 'extract_to_pdb',
        '--xtc', os.path.abspath(xtc_file_path),
        '--topology', os.path.abspath(topology_file_path),
        '--outfolder', os.path.abspath(output_pdb_dir),
        '--stride', str(stride),
        '--overwrite',
    ]

    try:
        _run_stage(self, cmd_extract, POCKETHUNTER_DIR, EXTRACT_TIMEOUT, 0, 25,
                   'Extracting frames', job_id=job_id)
    except Exception as e:
        _fail_job(self, job_id, 'extract', e, log_path=error_log_path)
        raise

    pdb_files = [f for f in os.listdir(output_pdb_dir) if f.endswith('.pdb')]
    self.update_state(state='PROGRESS', meta={
        'current_step': f'Extracted {len(pdb_files)} frames — starting pocket detection…',
        'progress': 25,
        'stage': 'detect',
        'frames_extracted': len(pdb_files),
    })

    # ── Stage 2: Detect pockets ──────────────────────────────────────────
    output_pockets_dir = os.path.join(output_folder_job, 'pockets')
    os.makedirs(output_pockets_dir, exist_ok=True)

    cmd_detect = [
        'python', POCKETHUNTER_CLI, 'detect_pockets',
        '--infolder', os.path.abspath(output_pdb_dir),
        '--outfolder', os.path.abspath(output_pockets_dir),
        '--numthreads', str(num_threads),
        '--compress',
        '--overwrite',
    ]

    try:
        _run_stage(self, cmd_detect, POCKETHUNTER_DIR, DETECT_TIMEOUT, 25, 60,
                   'Detecting pockets', job_id=job_id)
    except Exception as e:
        _fail_job(self, job_id, 'detect', e, log_path=error_log_path)
        raise

    pockets_csv = os.path.join(output_pockets_dir, 'pockets.csv')
    pockets_detected = 0
    if os.path.exists(pockets_csv):
        try:
            pockets_detected = len(pd.read_csv(pockets_csv))
        except Exception:
            pass

    # F1 — silent p2rank failure detection (exit 0 but missing/empty CSV).
    # Two distinct sub-cases share the same error type but get different
    # messages so the panel + live log can guide the user to the right
    # diagnosis path.
    if not os.path.exists(pockets_csv):
        err = DetectionProducedNoOutput(
            f"p2rank ran for job {job_id} but pockets.csv was never written. "
            "This is a silent crash — see the persisted stderr in "
            f"results/{job_id}/.live/detecting_pockets.stderr.log."
        )
        _fail_job(self, job_id, 'detect', err, log_path=error_log_path)
        raise err
    if pockets_detected == 0:
        err = DetectionProducedNoOutput(
            f"Detection completed but produced zero pockets for job {job_id} "
            "(pockets.csv has 0 rows). Two likely causes:\n"
            "  1. p2rank ran cleanly but found no pockets above its default "
            "probability threshold — common on small or flat-surface proteins "
            "(e.g. T4 lysozyme).\n"
            "  2. p2rank crashed mid-write and emitted only the CSV header.\n"
            f"Check the live log + results/{job_id}/.live/detecting_pockets.stderr.log "
            "to distinguish."
        )
        _fail_job(self, job_id, 'detect', err, log_path=error_log_path)
        raise err

    self.update_state(state='PROGRESS', meta={
        'current_step': f'Detected {pockets_detected} pockets — clustering…',
        'progress': 60,
        'stage': 'cluster',
        'pockets_detected': pockets_detected,
    })

    # ── Stage 3: Cluster pockets ─────────────────────────────────────────
    output_clusters_dir = os.path.join(output_folder_job, 'pocket_clusters')
    os.makedirs(output_clusters_dir, exist_ok=True)

    cmd_cluster = [
        'python', POCKETHUNTER_CLI, 'cluster_pockets',
        '--infile', os.path.abspath(pockets_csv),
        '--outfolder', os.path.abspath(output_clusters_dir),
        '--min_prob', str(min_prob),
        '--method', clustering_method,
        '--overwrite',
    ]
    if clustering_method == 'dbscan':
        cmd_cluster.append('--hierarchical')

    try:
        _run_stage(self, cmd_cluster, POCKETHUNTER_DIR, CLUSTER_TIMEOUT, 60, 80,
                   'Clustering pockets', job_id=job_id)
    except Exception as e:
        _fail_job(self, job_id, 'cluster', e, log_path=error_log_path)
        raise

    reps_csv = os.path.join(output_clusters_dir, 'cluster_representatives.csv')
    representatives = 0
    if os.path.exists(reps_csv):
        try:
            representatives = len(pd.read_csv(reps_csv))
        except Exception:
            pass

    # F2 — DBSCAN zero-clusters detection
    if not os.path.exists(reps_csv) or representatives == 0:
        err = ClusteringFoundNoClusters(
            f"Clustering produced no representatives for job {job_id} at min_prob={min_prob}. "
            "Lower min_prob (try 0.3 or 0.2), reduce the trajectory stride, "
            "or switch to the Hierarchical method."
        )
        _fail_job(self, job_id, 'cluster', err, log_path=error_log_path)
        raise err

    self.update_state(state='PROGRESS', meta={
        'current_step': f'Found {representatives} cluster representatives',
        'progress': 80,
        'stage': 'cluster_done',
        'representatives': representatives,
    })

    # v2 Phase B B2: generate viewer.cif from the PDB folder for Mol*.
    # Best-effort; failure surfaces in result_info, doesn't fail the pipeline.
    viewer_info = _write_viewer_file(job_id, output_pdb_dir)

    results_overview = {
        'status': 'completed',
        'output_folder': output_folder_job,
        'frames_extracted': len(pdb_files),
        'pockets_detected': pockets_detected,
        'representatives': representatives,
        'cluster_job_id': job_id,
        'processing_time': time.time() - pipeline_start,
        **viewer_info,
    }

    # ── Stage 4: Optional docking ────────────────────────────────────────
    if run_docking and ligand_folder and os.path.exists(reps_csv):
        self.update_state(state='PROGRESS', meta={
            'current_step': 'Starting molecular docking…',
            'progress': 80,
            'stage': 'docking',
        })
        try:
            from step4_docking import pdb_to_pdbqt, calc_box, run_smina, parse_smina_log
            from prody import parsePDB, writePDB
            import glob as _glob
            from docking_pair_failures import build_pair_failure_record
            from task_errors import NoPosesParsed

            df_rep = pd.read_csv(reps_csv)
            docking_out = os.path.join(output_folder_job, 'docking')
            os.makedirs(docking_out, exist_ok=True)
            pdb_source_dir = output_pdb_dir

            ligand_paths = sorted(_glob.glob(os.path.join(ligand_folder, '*.pdbqt')))
            total_pairs = max(1, len(df_rep) * len(ligand_paths))
            completed_pairs = 0
            list_outputs = []
            pair_failures: list[dict] = []

            for rec_idx, (_, pocket_row) in enumerate(df_rep.iterrows()):
                receptor_pdb_pred = pocket_row['File name']
                receptor_pdb = receptor_pdb_pred[:-12] if receptor_pdb_pred.endswith('_predictions') else receptor_pdb_pred
                receptor_pdb_path = os.path.join(pdb_source_dir, receptor_pdb)

                if not os.path.exists(receptor_pdb_path):
                    logger.warning(f"Receptor PDB not found, skipping: {receptor_pdb_path}")
                    miss_err = FileNotFoundError(f"Receptor PDB not found: {receptor_pdb_path}")
                    for lp in ligand_paths:
                        pair_failures.append(build_pair_failure_record(
                            os.path.basename(receptor_pdb), os.path.basename(lp), miss_err,
                        ))
                    completed_pairs += len(ligand_paths)
                    continue

                self.update_state(state='PROGRESS', meta={
                    'current_step': f'Preparing receptor {rec_idx + 1}/{len(df_rep)}: {receptor_pdb}',
                    'progress': 80 + int((completed_pairs / total_pairs) * 17),
                    'stage': 'docking',
                })

                syst = parsePDB(receptor_pdb_path)
                protein = syst.select('protein')
                protein_pdb = os.path.join(docking_out, os.path.basename(receptor_pdb))
                writePDB(protein_pdb, protein)
                receptor_pdbqt = protein_pdb[:-4] + '.pdbqt'
                pdb_to_pdbqt(protein_pdb, receptor_pdbqt, pH=ph_value)

                box_center, _, _ = calc_box(protein_pdb, pocket_row['residues'])
                box_size = [box_size_x, box_size_y, box_size_z]
                dock_folder = protein_pdb[:-4] + '_smina'
                os.makedirs(dock_folder, exist_ok=True)

                for lig_path in ligand_paths:
                    self.update_state(state='PROGRESS', meta={
                        'current_step': (
                            f'Docking {os.path.basename(lig_path)} → '
                            f'receptor {rec_idx + 1}/{len(df_rep)}'
                        ),
                        'progress': 80 + int((completed_pairs / total_pairs) * 17),
                        'stage': 'docking',
                        'pairs_done': completed_pairs,
                        'pairs_total': total_pairs,
                    })
                    out_path = os.path.join(dock_folder, os.path.basename(lig_path)[:-6] + '_smina.sdf')
                    try:
                        output_txt, _ = run_smina(
                            lig_path, receptor_pdbqt, out_path, box_center, box_size,
                            Config.SMINA_PATH, num_poses=num_poses, exhaustiveness=exhaustiveness,
                            log_dir=dock_folder,
                        )
                        df_out = parse_smina_log(output_txt)
                        if not df_out.empty:
                            df_out['ligand'] = os.path.basename(lig_path)[:-6]
                            df_out['receptor'] = os.path.basename(receptor_pdb)
                            df_out['receptor_path'] = receptor_pdbqt
                            df_out['receptor_pdb_path'] = protein_pdb
                            df_out['output_sdf'] = out_path
                            list_outputs.append(df_out)
                        else:
                            pair_failures.append(build_pair_failure_record(
                                os.path.basename(receptor_pdb),
                                os.path.basename(lig_path),
                                NoPosesParsed(
                                    "smina exited 0 but produced no parseable poses — "
                                    "likely a malformed input PDBQT or an empty result file."
                                ),
                                stderr_tail=output_txt[-1500:] if output_txt else "",
                            ))
                    except Exception as dock_pair_err:
                        logger.warning(f"Docking pair failed ({receptor_pdb} / {os.path.basename(lig_path)}): {dock_pair_err}")
                        pair_failures.append(build_pair_failure_record(
                            os.path.basename(receptor_pdb), os.path.basename(lig_path), dock_pair_err,
                        ))
                    completed_pairs += 1

            pair_failures_log = _write_pair_failure_log(job_id, pair_failures, total_pairs)
            pairs_failed = len(pair_failures)
            results_overview['pairs_total'] = total_pairs
            results_overview['pairs_succeeded'] = total_pairs - pairs_failed
            results_overview['pairs_failed'] = pairs_failed
            results_overview['pair_failures'] = pair_failures
            results_overview['pair_failures_log'] = pair_failures_log

            if list_outputs:
                df_dock = pd.concat(list_outputs, ignore_index=True)
                docking_results_file = os.path.join(docking_out, 'docking_results.csv')
                df_dock.to_csv(docking_results_file, index=False)
                results_overview['docking_poses'] = len(df_dock)
                results_overview['docking_results_file'] = docking_results_file
            else:
                results_overview['docking_poses'] = 0
        except Exception as dock_err:
            logger.warning(f"Optional docking step failed: {dock_err}")
            results_overview['docking_error'] = str(dock_err)

    _update_status_file(job_id, 'completed', 'Full pipeline completed', task_id=self.request.id,
                        result_info=results_overview)
    self.update_state(state='SUCCESS', meta=results_overview)
    return results_overview


@celery_app.task(bind=True)
def run_find_pockets_task(
    self,
    job_id,
    xtc_file_path=None,
    topology_file_path=None,
    pdb_input_dir=None,
    stride=10,
    num_threads=None,
):
    """Merged Step 1 + Step 2: extract frames (if needed) then detect pockets.

    Two input modes (validated via find_pockets_helpers.validate_find_pockets_inputs):
      - trajectory: xtc_file_path + topology_file_path → extract (0-50%) → detect (50-100%)
      - pdb_dir:    pdb_input_dir → detect (0-100%)

    Same on-disk layout and pockets.csv schema as the legacy two-task flow, so
    Step 2 (Cluster Pockets) consumes the output unchanged.
    """
    from find_pockets_helpers import progress_ranges, validate_find_pockets_inputs
    from task_errors import DetectionProducedNoOutput

    # B11.21: p2rank thread count is .env-driven (Config.P2RANK_THREADS),
    # not user-facing. ``num_threads`` is kept as an optional override.
    if num_threads is None:
        num_threads = Config.P2RANK_THREADS

    started = time.time()
    output_folder_job = os.path.join(RESULTS_DIR, job_id)
    os.makedirs(output_folder_job, exist_ok=True)

    # B11.17: flip the Job row submitted → running so DB-driven views
    # (jobs panel) reflect reality. update_state(PROGRESS) only writes
    # the Celery backend, not the Job row.
    _update_status_file(job_id, 'running', step='Find pockets started',
                        task_id=self.request.id)

    try:
        mode = validate_find_pockets_inputs(xtc_file_path, topology_file_path, pdb_input_dir)
    except ValueError as e:
        _fail_job(self, job_id, 'input_validation', e)
        raise

    (extract_lo, extract_hi), (detect_lo, detect_hi) = progress_ranges(mode)

    # ── Stage 1: Extract frames (trajectory mode only) ───────────────────
    if mode == 'trajectory':
        output_pdb_dir = os.path.join(output_folder_job, 'pdbs')
        os.makedirs(output_pdb_dir, exist_ok=True)

        self.update_state(state='PROGRESS', meta={
            'current_step': 'Extracting frames from trajectory…',
            'progress': extract_lo,
            'stage': 'extract',
        })

        cmd_extract = [
            'python', POCKETHUNTER_CLI, 'extract_to_pdb',
            '--xtc', os.path.abspath(xtc_file_path),
            '--topology', os.path.abspath(topology_file_path),
            '--outfolder', os.path.abspath(output_pdb_dir),
            '--stride', str(stride),
            '--overwrite',
        ]
        try:
            _run_stage(self, cmd_extract, POCKETHUNTER_DIR, EXTRACT_TIMEOUT,
                       extract_lo, extract_hi, 'Extracting frames', job_id=job_id)
        except Exception as e:
            _fail_job(self, job_id, 'extract', e,
                      log_path=os.path.join(output_folder_job, 'error.log'))
            raise

        pdb_files = [f for f in os.listdir(output_pdb_dir) if f.endswith('.pdb')]
        detect_infolder = output_pdb_dir
        frames_extracted = len(pdb_files)
    else:
        # pdb_dir mode — skip extraction, use the supplied directory directly.
        detect_infolder = os.path.abspath(pdb_input_dir)
        frames_extracted = len([f for f in os.listdir(detect_infolder) if f.endswith('.pdb')])

    self.update_state(state='PROGRESS', meta={
        'current_step': f'Starting pocket detection on {frames_extracted} structures…',
        'progress': detect_lo,
        'stage': 'detect',
        'frames_extracted': frames_extracted,
    })

    # ── Stage 2: Detect pockets ─────────────────────────────────────────
    output_pockets_dir = os.path.join(output_folder_job, 'pockets')
    os.makedirs(output_pockets_dir, exist_ok=True)

    cmd_detect = [
        'python', POCKETHUNTER_CLI, 'detect_pockets',
        '--infolder', detect_infolder,
        '--outfolder', os.path.abspath(output_pockets_dir),
        '--numthreads', str(num_threads),
        '--compress',
        '--overwrite',
    ]
    try:
        _run_stage(self, cmd_detect, POCKETHUNTER_DIR, DETECT_TIMEOUT,
                   detect_lo, detect_hi, 'Detecting pockets', job_id=job_id)
    except Exception as e:
        _fail_job(self, job_id, 'detect', e,
                  log_path=os.path.join(output_folder_job, 'error.log'))
        raise

    # F1 — catch silent p2rank failures: even on exit 0, the CSV may be
    # missing or empty (the underlying CLI suppresses p2rank's stderr).
    # Split missing-CSV (definite crash) from empty-CSV (could be either a
    # crash or a legitimate zero-pocket outcome) so the failure panel can
    # point the user at the right diagnostic.
    pockets_csv = os.path.join(output_pockets_dir, 'pockets.csv')
    pockets_detected = 0
    if os.path.exists(pockets_csv):
        try:
            pockets_detected = len(pd.read_csv(pockets_csv))
        except Exception:
            pockets_detected = 0
    if not os.path.exists(pockets_csv):
        err = DetectionProducedNoOutput(
            f"p2rank ran for job {job_id} but pockets.csv was never written. "
            "This is a silent crash — see the persisted stderr in "
            f"results/{job_id}/.live/detecting_pockets.stderr.log."
        )
        _fail_job(self, job_id, 'detect', err,
                  log_path=os.path.join(output_folder_job, 'error.log'))
        raise err
    if pockets_detected == 0:
        err = DetectionProducedNoOutput(
            f"Detection completed but produced zero pockets for job {job_id} "
            "(pockets.csv has 0 rows). Two likely causes:\n"
            "  1. p2rank ran cleanly but found no pockets above its default "
            "probability threshold — common on small or flat-surface proteins "
            "(e.g. T4 lysozyme).\n"
            "  2. p2rank crashed mid-write and emitted only the CSV header.\n"
            f"Check the live log + results/{job_id}/.live/detecting_pockets.stderr.log "
            "to distinguish."
        )
        _fail_job(self, job_id, 'detect', err,
                  log_path=os.path.join(output_folder_job, 'error.log'))
        raise err

    # v2 Phase B B2: generate viewer.cif from the PDB folder Mol* loads.
    # Best-effort; failure adds viewer_file_error to result_info but the
    # task still succeeds (analysis-side results are independent).
    viewer_info = _write_viewer_file(job_id, detect_infolder)

    elapsed = time.time() - started
    results_overview = {
        'status': 'completed',
        'mode': mode,
        'output_folder': output_folder_job,
        'pockets_output_dir_abs': output_pockets_dir,
        'pockets_csv_abs': pockets_csv,
        'frames_extracted': frames_extracted,
        'pockets_detected': pockets_detected,
        'processing_time': elapsed,
        **viewer_info,
    }

    _update_status_file(job_id, 'completed', 'Find pockets completed',
                        task_id=self.request.id,
                        result_info={
                            'mode': mode,
                            'frames_extracted': frames_extracted,
                            'pockets_detected': pockets_detected,
                            'processing_time': elapsed,
                            **viewer_info,
                        })
    self.update_state(state='SUCCESS', meta=results_overview)
    return results_overview

@celery_app.task(bind=True)
def run_cluster_pockets_task(self, pockets_csv_path_abs, job_id, min_prob, clustering_method, dbscan_hierarchical=True):
    """
    PocketHunter cluster_pockets step for Streamlit app.
    """
    self.update_state(
        state='PROGRESS', 
        meta={
            'current_step': 'Initializing pocket clustering...',
            'progress': 0,
            'status': 'Pocket clustering started'
        }
    )

    job_main_output_folder = os.path.join(RESULTS_DIR, job_id)
    os.makedirs(job_main_output_folder, exist_ok=True)

    output_clusters_dir = os.path.join(job_main_output_folder, "pocket_clusters")
    os.makedirs(output_clusters_dir, exist_ok=True)

    # B11.17: flip the Job row submitted → running (see find_pockets).
    _update_status_file(job_id, 'running', step='Pocket clustering started',
                        task_id=self.request.id)

    current_working_dir = POCKETHUNTER_DIR
    
    command = [
        'python', POCKETHUNTER_CLI,
        'cluster_pockets',
        '--infile', pockets_csv_path_abs,
        '--outfolder', os.path.abspath(output_clusters_dir),
        '--min_prob', str(min_prob),
        '--method', clustering_method,
        '--overwrite'
    ]

    if clustering_method == 'dbscan' and dbscan_hierarchical:
        command.append('--hierarchical')
    
    self.update_state(
        state='PROGRESS', 
        meta={
            'current_step': 'Clustering detected pockets',
            'progress': 10,
            'status': f'Executing: {" ".join(command)}'
        }
    )

    try:
        start_time = time.time()

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=current_working_dir,
            text=True,
            encoding='utf-8'
        )

        # Progress tracking variables
        progress = 5
        last_update = start_time
        update_interval = 2  # Update every 2 seconds

        # Monitor progress with real-time updates and timeout
        while process.poll() is None:
            time.sleep(1)
            current_time = time.time()
            elapsed = current_time - start_time

            # Check for timeout
            if elapsed > CLUSTER_TIMEOUT:
                process.kill()
                raise subprocess.TimeoutExpired(command, CLUSTER_TIMEOUT)

            # Update progress based on time elapsed (clustering is usually faster)
            if elapsed < 10:  # First 10 seconds - initialization
                progress = min(20, 5 + int((elapsed / 10) * 15))
            elif elapsed < 30:  # Next 20 seconds - processing
                progress = min(60, 20 + int(((elapsed - 10) / 20) * 40))
            elif elapsed < 60:  # Next 30 seconds - more processing
                progress = min(85, 60 + int(((elapsed - 30) / 30) * 25))
            else:  # After 1 minute - finishing up
                progress = min(95, 85 + int(((elapsed - 60) / 60) * 10))

            # Send progress update every few seconds
            if current_time - last_update >= update_interval:
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current_step': f'Analyzing pocket similarities (elapsed: {int(elapsed)}s)',
                        'progress': progress,
                        'status': f'Pocket clustering in progress... ({int(elapsed)}s elapsed)',
                        'elapsed': elapsed
                    }
                )
                last_update = current_time

        stdout, stderr = process.communicate(timeout=60)
        elapsed = time.time() - start_time

        if process.returncode == 0:
            # Find output files (check for both possible naming conventions)
            clustered_pockets_csv_abs = os.path.join(output_clusters_dir, 'pockets_clustered.csv')
            if not os.path.exists(clustered_pockets_csv_abs):
                clustered_pockets_csv_abs = os.path.join(output_clusters_dir, 'clustered_pockets.csv')
            
            representatives_csv_abs = os.path.join(output_clusters_dir, 'cluster_representatives.csv')
            if not os.path.exists(representatives_csv_abs):
                representatives_csv_abs = os.path.join(output_clusters_dir, 'representatives.csv')
            
            # List all files in the output directory for debugging
            import glob
            all_files = glob.glob(os.path.join(output_clusters_dir, '*.csv'))
            logger.debug(f"Found CSV files in {output_clusters_dir}: {all_files}")
            logger.debug(f"Looking for clustered_pockets_csv_abs: {clustered_pockets_csv_abs}")
            logger.debug(f"Looking for representatives_csv_abs: {representatives_csv_abs}")
            logger.debug(f"clustered_pockets_csv_abs exists: {os.path.exists(clustered_pockets_csv_abs)}")
            logger.debug(f"representatives_csv_abs exists: {os.path.exists(representatives_csv_abs)}")
            
            # Count results
            total_pockets = 0
            clusters_found = 0
            representatives = 0
            
            # Try to find files by pattern if exact names don't exist
            if not os.path.exists(clustered_pockets_csv_abs):
                # Look for any CSV file that might contain clustered pockets
                for csv_file in all_files:
                    if 'cluster' in csv_file.lower() and 'pocket' in csv_file.lower():
                        clustered_pockets_csv_abs = csv_file
                        break
            
            if not os.path.exists(representatives_csv_abs):
                # Look for any CSV file that might contain representatives
                for csv_file in all_files:
                    if 'representative' in csv_file.lower():
                        representatives_csv_abs = csv_file
                        break
            
            if os.path.exists(clustered_pockets_csv_abs):
                import pandas as pd
                try:
                    df = pd.read_csv(clustered_pockets_csv_abs)
                    total_pockets = len(df)
                    if 'cluster' in df.columns:
                        clusters_found = df['cluster'].nunique()
                    elif 'cluster_id' in df.columns:
                        clusters_found = df['cluster_id'].nunique()
                except Exception as e:
                    logger.debug(f"Error reading clustered_pockets_csv: {e}")
                    pass
            
            if os.path.exists(representatives_csv_abs):
                try:
                    df = pd.read_csv(representatives_csv_abs)
                    representatives = len(df)
                except Exception as e:
                    logger.debug(f"Error reading representatives_csv: {e}")
                    pass

            # F2 — Detect "DBSCAN found no clusters" as a real failure.
            # The subprocess returned 0, but if the representatives CSV is missing
            # or empty, the user got a useless success. Convert to an actionable error.
            if not os.path.exists(representatives_csv_abs) or representatives == 0:
                from task_errors import ClusteringFoundNoClusters
                err = ClusteringFoundNoClusters(
                    f"Clustering produced no representatives for job {job_id} at min_prob={min_prob}. "
                    "Lower min_prob (try 0.3 or 0.2), reduce the trajectory stride, "
                    "or switch to the Hierarchical method."
                )
                _fail_job(self, job_id, 'cluster', err,
                          log_path=os.path.join(RESULTS_DIR, job_id, 'error.log'))
                raise err

            # Final success update
            self.update_state(
                state='PROGRESS',
                meta={
                    'current_step': 'Pocket clustering completed',
                    'progress': 100,
                    'status': f'Successfully clustered {total_pockets} pockets into {clusters_found} clusters',
                    'elapsed': elapsed
                }
            )
            
            results_overview = {
                'status': 'completed',
                'clusters_output_dir_abs': output_clusters_dir,
                'clustered_pockets_csv_abs': clustered_pockets_csv_abs if os.path.exists(clustered_pockets_csv_abs) else None,
                'representatives_csv_abs': representatives_csv_abs if os.path.exists(representatives_csv_abs) else None,
                'total_pockets': total_pockets,
                'clusters_found': clusters_found,
                'representatives': representatives,
                'processing_time': elapsed,
                'stdout': stdout,
                'stderr': stderr
            }
            
            _update_status_file(job_id, 'completed', 'Pocket clustering completed successfully',
                task_id=self.request.id, result_info={
                    'total_pockets': total_pockets, 'clusters_found': clusters_found,
                    'representatives': representatives, 'processing_time': elapsed})
            self.update_state(state='SUCCESS', meta=results_overview)
            return results_overview
        else:
            error_message = f"Pocket clustering failed. Return code: {process.returncode}"
            _update_status_file(job_id, 'failed', error_message, task_id=self.request.id)
            meta = {
                'status': error_message,
                'stdout': stdout,
                'stderr': stderr,
                'output_folder': job_main_output_folder,
                'exc_type': 'Exception',
                'exc_message': f"{error_message}. Stderr: {stderr}"
            }
            self.update_state(state='FAILURE', meta=meta)
            raise Exception(f"{error_message}. Stderr: {stderr}")

    except subprocess.TimeoutExpired:
        error_message = f"Pocket clustering timed out after {CLUSTER_TIMEOUT} seconds"
        _update_status_file(job_id, 'failed', error_message, task_id=self.request.id)
        self.update_state(
            state='FAILURE',
            meta={
                'status': error_message,
                'output_folder': job_main_output_folder,
                'exc_type': 'TimeoutExpired',
                'exc_message': error_message
            }
        )
        raise Exception(error_message)
    except Exception as e:
        _update_status_file(job_id, 'failed', str(e), task_id=self.request.id)
        meta = {
            'status': f'Error occurred: {str(e)}',
            'output_folder': job_main_output_folder,
            'exc_type': type(e).__name__,
            'exc_message': str(e)
        }
        if hasattr(e, 'stdout'): meta['stdout'] = e.stdout
        if hasattr(e, 'stderr'): meta['stderr'] = e.stderr
        self.update_state(state='FAILURE', meta=meta)
        raise


@celery_app.task(
    bind=True,
    queue='docking',
    soft_time_limit=Config.DOCKING_TIMEOUT,
    time_limit=Config.DOCKING_TIMEOUT + 300,
)
def run_docking_task(self, cluster_representatives_csv, ligand_folder, job_id, num_poses=10, exhaustiveness=8, ph_value=7.4, pdb_source_dir=None, gen_3d=False, scoring_function="vinardo"):
    """
    Molecular docking task for Streamlit app.

    B11.16: the docking box is now computed per pocket from each
    pocket's residue bounding cloud (no user box sliders), smina
    resolves on PATH (no ``smina_exe_path``), and a scoring function
    is selectable. A partial ``docking_results.csv`` is written after
    every successful pair so the panel can show a live score grid.

    Parameters
    ----------
    cluster_representatives_csv : str
        Path to CSV file containing the selected docking pockets.
    ligand_folder : str
        Path to folder containing ligand files (SDF/PDB converted to
        PDBQT in the prep stage).
    job_id : str
        Unique job identifier.
    num_poses : int
        Maximum number of poses per docking.
    exhaustiveness : int
        Docking accuracy parameter.
    scoring_function : str
        smina ``--scoring`` value (vinardo / vina / ad4_scoring /
        dkoes_scoring). Passed through to :func:`step4_docking.run_smina`.
    ph_value : float
        pH for receptor protonation.
    pdb_source_dir : str, optional
        Directory containing source PDB files for receptors.
    gen_3d : bool
        When true, OpenBabel runs ``--gen3d`` in the ligand prep stage.
    """
    start_time = time.time()
    output_folder_job = os.path.join(RESULTS_DIR, f'dock_{job_id}')
    os.makedirs(output_folder_job, exist_ok=True)

    # B11.20: live log. The PocketHunter CLI stages get theirs from
    # ``_run_stage``; the docking loop runs smina inline, so write our
    # own ``.live/docking.stdout.log`` under ``RESULTS_DIR/<job_id>/``
    # (the path ``live_log.find_active_stage_log`` globs — note it's the
    # bare job_id, NOT the ``dock_<job_id>`` output dir).
    _live_dir = os.path.join(RESULTS_DIR, job_id, '.live')
    os.makedirs(_live_dir, exist_ok=True)
    _live_log_path = os.path.join(_live_dir, 'docking.stdout.log')
    open(_live_log_path, 'w').close()  # truncate any previous run

    def _live(line):
        """Append a line to the docking live log (best-effort)."""
        try:
            with open(_live_log_path, 'a') as _fh:
                _fh.write(line.rstrip('\n') + '\n')
        except OSError:
            pass

    # B11.17: flip the Job row submitted → running (see find_pockets).
    _update_status_file(job_id, 'running', step='Docking job started',
                        task_id=self.request.id)

    # B11.16: smina is on PATH (conda env baked into the image).
    smina_exe_path = "smina"

    self.update_state(state='PROGRESS', meta={
        'current_step': 'Reading cluster representatives…',
        'progress': 2,
        'pairs_done': 0,
        'pairs_total': 0,
        'elapsed': time.time() - start_time,
    })

    try:
        from step4_docking import pdb_to_pdbqt, calc_box, run_smina, parse_smina_log
        from prody import parsePDB, writePDB
        import glob as _glob
        from docking_pair_failures import build_pair_failure_record
        from task_errors import NoPosesParsed

        df_rep_pockets = pd.read_csv(cluster_representatives_csv)

        required_columns = {'File name', 'residues'}
        missing = required_columns - set(df_rep_pockets.columns)
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}. Found: {list(df_rep_pockets.columns)}")

        # Resolve PDB source directory if not supplied: fall back to the most
        # recent extract_* job under results/.
        if pdb_source_dir is None:
            extract_dirs = sorted(
                [d for d in os.listdir(RESULTS_DIR) if d.startswith('extract_')
                 and os.path.isdir(os.path.join(RESULTS_DIR, d))]
            )
            if not extract_dirs:
                raise FileNotFoundError("No extract directories found. Provide pdb_source_dir.")
            pdb_source_dir = os.path.join(RESULTS_DIR, extract_dirs[-1], 'pdbs')

        # B11.12: convert any SDF/PDB inputs to PDBQT here, with
        # progress updates. The panel just saves files; conversion
        # happens server-side so obabel runtime appears on the
        # docking progress bar (2-15% of total).
        ligand_conversion_failures, ligand_names = _prepare_ligands_with_progress(
            self, ligand_folder, gen_3d=gen_3d, progress_low=2, progress_high=15,
        )
        ligand_paths = sorted(_glob.glob(os.path.join(ligand_folder, '*.pdbqt')))
        if not ligand_paths:
            raise FileNotFoundError(
                f"No usable PDBQT ligand files in {ligand_folder} after prep."
            )

        n_receptors = len(df_rep_pockets)
        n_ligands = len(ligand_paths)
        total_pairs = n_receptors * n_ligands
        completed_pairs = 0
        pair_failures: list[dict] = []

        _start_msg = (
            f'Starting docking: {n_receptors} receptors × '
            f'{n_ligands} ligands = {total_pairs} pairs'
        )
        self.update_state(state='PROGRESS', meta={
            'current_step': _start_msg,
            'progress': 5,
            'pairs_done': 0,
            'pairs_total': total_pairs,
            'elapsed': time.time() - start_time,
        })
        _live(_start_msg)

        list_outputs = []
        prepped: list[dict] = []

        # ── Phase 1: prepare every receptor once (progress 5 → 15) ──
        # Prep is hoisted out of the docking loop so Phase 2 can run
        # ligand-outer (one ligand scored against every receptor before
        # the next). That fills the score grid row-by-row — what
        # ensemble docking wants to observe — instead of column-by-column.
        for rec_idx, (_, pocket_row) in enumerate(df_rep_pockets.iterrows()):
            receptor_pdb_pred = pocket_row['File name']
            receptor_pdb = receptor_pdb_pred[:-12] if receptor_pdb_pred.endswith('_predictions') else receptor_pdb_pred
            receptor_pdb_path = os.path.join(pdb_source_dir, receptor_pdb)

            # Progress: 5 → 15 during receptor preparation
            _prep_msg = f'Preparing receptor {rec_idx + 1}/{n_receptors}: {receptor_pdb}'
            self.update_state(state='PROGRESS', meta={
                'current_step': _prep_msg,
                'progress': 5 + int((rec_idx / n_receptors) * 10),
                'pairs_done': completed_pairs,
                'pairs_total': total_pairs,
                'elapsed': time.time() - start_time,
            })
            _live(_prep_msg)

            if not os.path.exists(receptor_pdb_path):
                logger.warning(f"Receptor PDB not found, skipping: {receptor_pdb_path}")
                miss_err = FileNotFoundError(f"Receptor PDB not found: {receptor_pdb_path}")
                for lig_path in ligand_paths:
                    pair_failures.append(build_pair_failure_record(
                        os.path.basename(receptor_pdb), os.path.basename(lig_path), miss_err,
                    ))
                completed_pairs += n_ligands
                continue

            try:
                syst = parsePDB(receptor_pdb_path)
                protein = syst.select('protein')
                protein_pdb = os.path.join(output_folder_job, os.path.basename(receptor_pdb))
                writePDB(protein_pdb, protein)
                receptor_pdbqt = protein_pdb[:-4] + '.pdbqt'
                pdb_to_pdbqt(protein_pdb, receptor_pdbqt, pH=ph_value)
                # B11.16: per-pocket box. ``calc_box`` returns the
                # residue cloud's center + min/max corners; size it to
                # the bounding box + 4 Å padding, clamped to [10, 50].
                box_center, box_min, box_max = calc_box(protein_pdb, pocket_row['residues'])
                _pad = 4.0
                box_size = [
                    float(min(max(box_max[i] - box_min[i] + 2 * _pad, 10.0), 50.0))
                    for i in range(3)
                ]
                dock_folder = protein_pdb[:-4] + '_smina'
                os.makedirs(dock_folder, exist_ok=True)
            except Exception as prep_err:
                # Receptor prep failed — count every ligand pair against this
                # receptor as failed so the user sees what happened.
                logger.warning(f"Receptor prep failed ({receptor_pdb}): {prep_err}. Skipping.")
                for lig_path in ligand_paths:
                    pair_failures.append(build_pair_failure_record(
                        os.path.basename(receptor_pdb), os.path.basename(lig_path), prep_err,
                    ))
                completed_pairs += n_ligands
                continue

            prepped.append({
                'receptor_pdb': receptor_pdb,
                'receptor_pdbqt': receptor_pdbqt,
                'protein_pdb': protein_pdb,
                'box_center': box_center,
                'box_size': box_size,
                'dock_folder': dock_folder,
            })

        # ── Phase 2: dock ligand-outer, receptor-inner (15 → 95) ──
        # One ligand is scored against every prepped receptor before the
        # next ligand, so the incremental docking_results.csv grows a
        # full ligand row at a time.
        for lig_idx, lig_path in enumerate(ligand_paths):
            for rec in prepped:
                # Progress: 15 → 95 across all pairs
                progress = 15 + int((completed_pairs / total_pairs) * 80)

                _dock_msg = (
                    f'Docking ligand {lig_idx + 1}/{n_ligands} '
                    f'({os.path.basename(lig_path)}) → {rec["receptor_pdb"]} '
                    f'[{completed_pairs + 1}/{total_pairs}]'
                )
                self.update_state(state='PROGRESS', meta={
                    'current_step': _dock_msg,
                    'progress': progress,
                    'pairs_done': completed_pairs,
                    'pairs_total': total_pairs,
                    'receptor': rec['receptor_pdb'],
                    'ligand': os.path.basename(lig_path),
                    'elapsed': time.time() - start_time,
                })
                _live(_dock_msg)

                out_path = os.path.join(rec['dock_folder'], os.path.basename(lig_path)[:-6] + '_smina.sdf')
                try:
                    output_txt, _ = run_smina(
                        lig_path, rec['receptor_pdbqt'], out_path,
                        rec['box_center'], rec['box_size'],
                        smina_exe_path, num_poses=num_poses, exhaustiveness=exhaustiveness,
                        log_dir=rec['dock_folder'], scoring_function=scoring_function,
                    )
                    df_out = parse_smina_log(output_txt)
                    if not df_out.empty:
                        _lig_stem = os.path.basename(lig_path)[:-6]
                        df_out['ligand'] = _lig_stem
                        # B11.21: real molecule name (falls back to the
                        # PDBQT stem) so the docking grid is meaningful.
                        df_out['ligand_name'] = ligand_names.get(_lig_stem, _lig_stem)
                        df_out['receptor'] = os.path.basename(rec['receptor_pdb'])
                        df_out['receptor_path'] = rec['receptor_pdbqt']
                        df_out['receptor_pdb_path'] = rec['protein_pdb']
                        df_out['output_sdf'] = out_path
                        list_outputs.append(df_out)
                        # B11.16: write an incremental snapshot so the
                        # panel's running state can render the partial
                        # score grid. Atomic via temp-file + os.replace.
                        try:
                            _partial = os.path.join(output_folder_job, 'docking_results.csv')
                            _tmp = _partial + '.tmp'
                            pd.concat(list_outputs, ignore_index=True).to_csv(_tmp, index=False)
                            os.replace(_tmp, _partial)
                            # B11.17: mirror the partial path + progress
                            # into the Job row so DB readers (jobs panel,
                            # admin views) see live docking progress.
                            _update_status_file(
                                job_id, 'running',
                                step=(
                                    f'Docked {completed_pairs + 1}/{total_pairs} '
                                    'ligand-pocket pairs'
                                ),
                                task_id=self.request.id,
                                result_info={
                                    'docking_results_file': _partial,
                                    'pairs_done': completed_pairs + 1,
                                    'pairs_total': total_pairs,
                                },
                            )
                        except Exception as _snap_err:
                            logger.warning(f"partial results snapshot failed: {_snap_err}")
                    else:
                        # smina exited cleanly but produced nothing parseable.
                        pair_failures.append(build_pair_failure_record(
                            os.path.basename(rec['receptor_pdb']),
                            os.path.basename(lig_path),
                            NoPosesParsed(
                                "smina exited 0 but produced no parseable poses — "
                                "likely a malformed input PDBQT or an empty result file."
                            ),
                            stderr_tail=output_txt[-1500:] if output_txt else "",
                        ))
                except SoftTimeLimitExceeded:
                    elapsed = time.time() - start_time
                    _update_status_file(job_id, 'failed',
                                        f'Docking timed out after {elapsed:.0f}s', task_id=self.request.id)
                    self.update_state(state='FAILURE', meta={
                        'status': f'Docking timed out after {elapsed:.0f}s',
                        'exc_type': 'SoftTimeLimitExceeded',
                        'exc_message': 'Docking exceeded time limit',
                        'pairs_done': completed_pairs,
                        'pairs_total': total_pairs,
                    })
                    raise
                except Exception as pair_err:
                    logger.warning(f"Pair failed ({rec['receptor_pdb']} / {os.path.basename(lig_path)}): {pair_err}")
                    pair_failures.append(build_pair_failure_record(
                        os.path.basename(rec['receptor_pdb']), os.path.basename(lig_path), pair_err,
                    ))

                completed_pairs += 1

        pair_failures_log = _write_pair_failure_log(job_id, pair_failures, total_pairs)

        if not list_outputs:
            raise ValueError("No docking results generated. Check that ligands and receptors are valid.")

        df_outputs = pd.concat(list_outputs, ignore_index=True)
        docking_results_file = os.path.join(output_folder_job, 'docking_results.csv')
        df_outputs.to_csv(docking_results_file, index=False)

        elapsed = time.time() - start_time
        total_docking_poses = len(df_outputs)
        unique_ligands = df_outputs['ligand'].nunique()
        unique_receptors = df_outputs['receptor'].nunique()
        best_affinity = df_outputs['affinity (kcal/mol)'].min()

        pairs_failed = len(pair_failures)
        pairs_succeeded = total_pairs - pairs_failed
        results_overview = {
            'status': 'completed',
            'docking_output_dir': output_folder_job,
            'docking_results_file': docking_results_file,
            'total_docking_poses': total_docking_poses,
            'unique_ligands': unique_ligands,
            'unique_receptors': unique_receptors,
            'best_affinity': best_affinity,
            'processing_time': elapsed,
            'num_poses': num_poses,
            'exhaustiveness': exhaustiveness,
            'pairs_done': completed_pairs,
            'pairs_total': total_pairs,
            'pairs_succeeded': pairs_succeeded,
            'pairs_failed': pairs_failed,
            'pair_failures': pair_failures,
            'pair_failures_log': pair_failures_log,
            'ligand_conversion_failures': ligand_conversion_failures,
        }

        _update_status_file(job_id, 'completed', 'Molecular docking completed successfully',
                            task_id=self.request.id, result_info=results_overview)
        self.update_state(state='SUCCESS', meta=results_overview)
        return results_overview

    except Exception as e:
        elapsed = time.time() - start_time
        _update_status_file(job_id, 'failed', str(e), task_id=self.request.id)
        self.update_state(state='FAILURE', meta={
            'status': f'Error: {str(e)}',
            'output_folder': output_folder_job,
            'exc_type': type(e).__name__,
            'exc_message': str(e),
            'processing_time': elapsed,
        })
        raise