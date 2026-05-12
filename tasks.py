import subprocess
import os
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
    """Update the job status JSON file on disk.

    On failure, pass an ``error`` dict containing at minimum ``exc_type``,
    ``exc_message``, and ``stage`` so the UI can render a structured failure
    panel without consulting the Celery result backend (which expires).
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


def validate_pockethunter_output(output_dir, expected_files=None, expected_dirs=None):
    """
    Validate that PocketHunter output exists and contains expected files.

    Parameters
    ----------
    output_dir : str
        Path to the output directory to validate.
    expected_files : list of str, optional
        List of expected file names or patterns to check for.
    expected_dirs : list of str, optional
        List of expected subdirectory names to check for.

    Returns
    -------
    dict
        Validation results with 'valid' boolean, 'missing_files', 'missing_dirs', and 'found_files'.
    """
    result = {
        'valid': True,
        'missing_files': [],
        'missing_dirs': [],
        'found_files': [],
        'output_dir': output_dir
    }

    # Check if output directory exists
    if not os.path.exists(output_dir):
        result['valid'] = False
        logger.warning(f"Output directory does not exist: {output_dir}")
        return result

    # Check expected subdirectories
    if expected_dirs:
        for subdir in expected_dirs:
            subdir_path = os.path.join(output_dir, subdir)
            if not os.path.exists(subdir_path):
                result['valid'] = False
                result['missing_dirs'].append(subdir)
                logger.warning(f"Expected directory missing: {subdir_path}")

    # Check expected files
    if expected_files:
        for filename in expected_files:
            file_path = os.path.join(output_dir, filename)
            if os.path.exists(file_path):
                result['found_files'].append(filename)
            else:
                result['valid'] = False
                result['missing_files'].append(filename)
                logger.warning(f"Expected file missing: {file_path}")

    return result


def validate_csv_output(csv_path, required_columns=None, min_rows=0):
    """
    Validate that a CSV output file exists and has expected structure.

    Parameters
    ----------
    csv_path : str
        Path to the CSV file to validate.
    required_columns : list of str, optional
        List of column names that must exist in the CSV.
    min_rows : int
        Minimum number of data rows expected.

    Returns
    -------
    dict
        Validation results with 'valid' boolean, 'row_count', 'missing_columns', and 'error'.
    """
    result = {
        'valid': True,
        'row_count': 0,
        'missing_columns': [],
        'error': None
    }

    if not os.path.exists(csv_path):
        result['valid'] = False
        result['error'] = f"CSV file does not exist: {csv_path}"
        logger.warning(result['error'])
        return result

    try:
        df = pd.read_csv(csv_path)
        result['row_count'] = len(df)

        # Check minimum row count
        if len(df) < min_rows:
            result['valid'] = False
            result['error'] = f"CSV has {len(df)} rows, expected at least {min_rows}"
            logger.warning(result['error'])

        # Check required columns
        if required_columns:
            for col in required_columns:
                if col not in df.columns:
                    result['valid'] = False
                    result['missing_columns'].append(col)
                    logger.warning(f"Required column missing in {csv_path}: {col}")

    except Exception as e:
        result['valid'] = False
        result['error'] = f"Error reading CSV: {str(e)}"
        logger.error(result['error'])

    return result

def _run_stage(celery_task, command, cwd, timeout, prog_start, prog_end, stage_name,
               update_interval=2, job_id=None):
    """
    Run a subprocess stage and poll it, emitting Celery progress updates within [prog_start, prog_end].

    stdout/stderr are redirected to temp files to avoid OS pipe-buffer deadlocks that
    occur when a subprocess writes more than ~64 KB without being drained.

    On non-zero exit *and* when ``job_id`` is supplied, the captured stderr is also
    written to ``results/<job_id>/error.log`` before the temp files are cleaned up,
    so the UI can offer it as a download.

    Returns (stdout_text, stderr_text) on success; raises Exception on failure or timeout.
    """
    import tempfile

    start_time = time.time()
    prog_range = prog_end - prog_start

    # Use temp files instead of PIPE to prevent deadlock when subprocess output is large
    fout = tempfile.NamedTemporaryFile(mode='w', suffix='_stdout.txt', delete=False)
    ferr = tempfile.NamedTemporaryFile(mode='w', suffix='_stderr.txt', delete=False)
    stdout_path = fout.name
    stderr_path = ferr.name
    fout.close()
    ferr.close()

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
        for p in (stdout_path, stderr_path):
            try:
                os.unlink(p)
            except OSError:
                pass


@celery_app.task(bind=True)
def run_pockethunter_pipeline(self, xtc_file_path, topology_file_path, job_id, stride=10, num_threads=4,
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

    # F1 — silent p2rank failure detection (exit 0 but missing/empty CSV)
    if not os.path.exists(pockets_csv) or pockets_detected == 0:
        err = DetectionProducedNoOutput(
            f"Pocket detection produced no usable output for job {job_id}. "
            f"pockets.csv exists={os.path.exists(pockets_csv)}, rows={pockets_detected}. "
            "p2rank may have crashed silently — see the error log."
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

    results_overview = {
        'status': 'completed',
        'output_folder': output_folder_job,
        'frames_extracted': len(pdb_files),
        'pockets_detected': pockets_detected,
        'representatives': representatives,
        'cluster_job_id': job_id,
        'processing_time': time.time() - pipeline_start,
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
    num_threads=4,
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

    started = time.time()
    output_folder_job = os.path.join(RESULTS_DIR, job_id)
    os.makedirs(output_folder_job, exist_ok=True)

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
    pockets_csv = os.path.join(output_pockets_dir, 'pockets.csv')
    pockets_detected = 0
    if os.path.exists(pockets_csv):
        try:
            pockets_detected = len(pd.read_csv(pockets_csv))
        except Exception:
            pockets_detected = 0
    if not os.path.exists(pockets_csv) or pockets_detected == 0:
        err = DetectionProducedNoOutput(
            f"Pocket detection produced no usable output for job {job_id}. "
            f"pockets.csv exists={os.path.exists(pockets_csv)}, rows={pockets_detected}. "
            "p2rank may have crashed silently — see the error log."
        )
        _fail_job(self, job_id, 'detect', err,
                  log_path=os.path.join(output_folder_job, 'error.log'))
        raise err

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
    }

    _update_status_file(job_id, 'completed', 'Find pockets completed',
                        task_id=self.request.id,
                        result_info={
                            'mode': mode,
                            'frames_extracted': frames_extracted,
                            'pockets_detected': pockets_detected,
                            'processing_time': elapsed,
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
def run_docking_task(self, cluster_representatives_csv, ligand_folder, job_id, smina_exe_path=None, num_poses=10, exhaustiveness=8, ph_value=7.4, box_size_x=20.0, box_size_y=20.0, box_size_z=20.0, pdb_source_dir=None):
    """
    Molecular docking task for Streamlit app.

    Parameters
    ----------
    cluster_representatives_csv : str
        Path to CSV file containing cluster representatives.
    ligand_folder : str
        Path to folder containing ligand PDBQT files.
    job_id : str
        Unique job identifier.
    smina_exe_path : str, optional
        Path to smina executable.
    num_poses : int
        Maximum number of poses per docking.
    exhaustiveness : int
        Docking accuracy parameter.
    ph_value : float
        pH for protonation.
    box_size_x, box_size_y, box_size_z : float
        Docking box dimensions.
    pdb_source_dir : str, optional
        Directory containing source PDB files for receptors.
    """
    start_time = time.time()
    output_folder_job = os.path.join(RESULTS_DIR, f'dock_{job_id}')
    os.makedirs(output_folder_job, exist_ok=True)

    if smina_exe_path is None:
        smina_exe_path = Config.SMINA_PATH

    self.update_state(state='PROGRESS', meta={
        'current_step': 'Reading cluster representatives…',
        'progress': 2,
        'pairs_done': 0,
        'pairs_total': 0,
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

        # Resolve PDB source directory (same logic as dock_ensemble)
        if pdb_source_dir is None:
            extract_dirs = sorted(
                [d for d in os.listdir(RESULTS_DIR) if d.startswith('extract_')
                 and os.path.isdir(os.path.join(RESULTS_DIR, d))]
            )
            if not extract_dirs:
                raise FileNotFoundError("No extract directories found. Provide pdb_source_dir.")
            pdb_source_dir = os.path.join(RESULTS_DIR, extract_dirs[-1], 'pdbs')

        ligand_paths = sorted(_glob.glob(os.path.join(ligand_folder, '*.pdbqt')))
        if not ligand_paths:
            raise FileNotFoundError(f"No PDBQT ligand files found in {ligand_folder}")

        n_receptors = len(df_rep_pockets)
        n_ligands = len(ligand_paths)
        total_pairs = n_receptors * n_ligands
        completed_pairs = 0
        pair_failures: list[dict] = []

        self.update_state(state='PROGRESS', meta={
            'current_step': f'Starting docking: {n_receptors} receptors × {n_ligands} ligands = {total_pairs} pairs',
            'progress': 5,
            'pairs_done': 0,
            'pairs_total': total_pairs,
        })

        list_outputs = []

        for rec_idx, (_, pocket_row) in enumerate(df_rep_pockets.iterrows()):
            receptor_pdb_pred = pocket_row['File name']
            receptor_pdb = receptor_pdb_pred[:-12] if receptor_pdb_pred.endswith('_predictions') else receptor_pdb_pred
            receptor_pdb_path = os.path.join(pdb_source_dir, receptor_pdb)

            if not os.path.exists(receptor_pdb_path):
                logger.warning(f"Receptor PDB not found, skipping: {receptor_pdb_path}")
                completed_pairs += n_ligands
                continue

            # Progress: 5 → 15 during receptor preparation
            prep_progress = 5 + int((rec_idx / n_receptors) * 10)
            self.update_state(state='PROGRESS', meta={
                'current_step': f'Preparing receptor {rec_idx + 1}/{n_receptors}: {receptor_pdb}',
                'progress': prep_progress,
                'pairs_done': completed_pairs,
                'pairs_total': total_pairs,
            })

            try:
                syst = parsePDB(receptor_pdb_path)
                protein = syst.select('protein')
                protein_pdb = os.path.join(output_folder_job, os.path.basename(receptor_pdb))
                writePDB(protein_pdb, protein)
                receptor_pdbqt = protein_pdb[:-4] + '.pdbqt'
                pdb_to_pdbqt(protein_pdb, receptor_pdbqt, pH=ph_value)
                box_center, _, _ = calc_box(protein_pdb, pocket_row['residues'])
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

            box_size = [box_size_x, box_size_y, box_size_z]
            dock_folder = protein_pdb[:-4] + '_smina'
            os.makedirs(dock_folder, exist_ok=True)

            for lig_path in ligand_paths:
                # Progress: 15 → 95 across all pairs
                pair_frac = completed_pairs / total_pairs
                progress = 15 + int(pair_frac * 80)

                self.update_state(state='PROGRESS', meta={
                    'current_step': (
                        f'Docking {os.path.basename(lig_path)} → '
                        f'receptor {rec_idx + 1}/{n_receptors} '
                        f'({completed_pairs + 1}/{total_pairs})'
                    ),
                    'progress': progress,
                    'pairs_done': completed_pairs,
                    'pairs_total': total_pairs,
                    'receptor': receptor_pdb,
                    'ligand': os.path.basename(lig_path),
                })

                out_path = os.path.join(dock_folder, os.path.basename(lig_path)[:-6] + '_smina.sdf')
                try:
                    output_txt, _ = run_smina(
                        lig_path, receptor_pdbqt, out_path, box_center, box_size,
                        smina_exe_path, num_poses=num_poses, exhaustiveness=exhaustiveness,
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
                        # smina exited cleanly but produced nothing parseable.
                        pair_failures.append(build_pair_failure_record(
                            os.path.basename(receptor_pdb),
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
                    logger.warning(f"Pair failed ({receptor_pdb} / {os.path.basename(lig_path)}): {pair_err}")
                    pair_failures.append(build_pair_failure_record(
                        os.path.basename(receptor_pdb), os.path.basename(lig_path), pair_err,
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