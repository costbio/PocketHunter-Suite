import subprocess
import os
import uuid
import shutil 
import json
from celery_app import celery_app 
import time
from datetime import datetime
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POCKETHUNTER_DIR = os.path.join(BASE_DIR, 'PocketHunter')
POCKETHUNTER_CLI = os.path.join(POCKETHUNTER_DIR, 'pockethunter.py')
RESULTS_DIR = os.path.join(BASE_DIR, 'results')

os.makedirs(RESULTS_DIR, exist_ok=True)

@celery_app.task(bind=True)
def run_pockethunter_pipeline(self, xtc_file_path, topology_file_path, job_id, stride=10, num_threads=4, min_prob=0.5, clustering_method='dbscan'):
    """
    PocketHunter full pipeline task for Streamlit app.
    """
    self.update_state(
        state='PROGRESS', 
        meta={
            'current_step': 'Initializing pipeline...',
            'progress': 0,
            'status': 'Pipeline started'
        }
    )

    output_folder_job = os.path.join(RESULTS_DIR, job_id)
    os.makedirs(output_folder_job, exist_ok=True)
    
    current_working_dir = POCKETHUNTER_DIR
    
    command = [
        'python', POCKETHUNTER_CLI,
        'full_pipeline',
        '--xtc', os.path.abspath(xtc_file_path), 
        '--topology', os.path.abspath(topology_file_path), 
        '--outfolder', os.path.abspath(output_folder_job), 
        '--stride', str(stride),
        '--numthreads', str(num_threads),
        '--min_prob', str(min_prob),
        '--method', clustering_method,
        '--compress',
        '--overwrite'
    ]
    if clustering_method == 'dbscan': 
        command.append('--hierarchical')

    self.update_state(
        state='PROGRESS', 
        meta={
            'current_step': 'Running PocketHunter full pipeline',
            'progress': 10,
            'status': f'Executing: {" ".join(command)}'
        }
    )

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=current_working_dir, 
            text=True,
            encoding='utf-8'
        )
        
        # Monitor progress
        progress = 10
        while process.poll() is None:
            time.sleep(5)
            progress = min(90, progress + 10)
            self.update_state(
                state='PROGRESS',
                meta={
                    'current_step': 'Processing molecular dynamics data',
                    'progress': progress,
                    'status': 'Pipeline running...'
                }
            )
        
        stdout, stderr = process.communicate()

        if process.returncode == 0:
            # Collect results
            output_files = []
            if os.path.exists(output_folder_job):
                for root, dirs, files in os.walk(output_folder_job):
                    for file in files:
                        if file.endswith(('.csv', '.pdb', '.png', '.jpg', '.pdf')):
                            output_files.append(os.path.join(root, file))
            
            results_overview = {
                'status': 'completed',
                'output_folder': output_folder_job,
                'output_files': output_files,
                'frames_extracted': len([f for f in output_files if f.endswith('.pdb')]),
                'pockets_detected': 0,  # Will be calculated from pockets.csv
                'clusters_found': 0,    # Will be calculated from clustered data
                'representatives': 0,    # Will be calculated from representatives
                'processing_time': time.time(),
                'stdout': stdout,
                'stderr': stderr
            }
            
            # Calculate metrics from output files
            pockets_csv = os.path.join(output_folder_job, 'pockets', 'pockets.csv')
            if os.path.exists(pockets_csv):
                import pandas as pd
                try:
                    df = pd.read_csv(pockets_csv)
                    results_overview['pockets_detected'] = len(df)
                except:
                    pass
            
            clustered_csv = os.path.join(output_folder_job, 'pocket_clusters', 'pockets_clustered.csv')
            if os.path.exists(clustered_csv):
                import pandas as pd
                try:
                    df = pd.read_csv(clustered_csv)
                    results_overview['clusters_found'] = df['cluster_id'].nunique() if 'cluster_id' in df.columns else 0
                except:
                    pass
            
            reps_csv = os.path.join(output_folder_job, 'pocket_clusters', 'cluster_representatives.csv')
            if os.path.exists(reps_csv):
                import pandas as pd
                try:
                    df = pd.read_csv(reps_csv)
                    results_overview['representatives'] = len(df)
                except:
                    pass
            
            self.update_state(state='SUCCESS', meta=results_overview)
            return results_overview
        else:
            error_message = f"PocketHunter pipeline failed. Return code: {process.returncode}"
            self.update_state(
                state='FAILURE', 
                meta={
                    'status': error_message, 
                    'stdout': stdout, 
                    'stderr': stderr, 
                    'output_folder': output_folder_job
                }
            )
            raise Exception(f"{error_message}. Stderr: {stderr}")

    except FileNotFoundError as e:
        self.update_state(
            state='FAILURE', 
            meta={
                'status': 'Error: pockethunter.py or python not found. Ensure PocketHunter is properly installed.',
                'exc_type': type(e).__name__,
                'exc_message': str(e)
            }
        )
        raise
    except Exception as e:
        meta = {
            'status': f'Unexpected error occurred: {str(e)}', 
            'output_folder': output_folder_job,
            'exc_type': type(e).__name__,
            'exc_message': str(e)
        }
        if hasattr(e, 'stdout'): meta['stdout'] = e.stdout
        if hasattr(e, 'stderr'): meta['stderr'] = e.stderr
        self.update_state(state='FAILURE', meta=meta)
        raise


@celery_app.task(bind=True)
def run_extract_to_pdb_task(self, xtc_file_path, topology_file_path, job_id, stride, num_threads):
    """
    PocketHunter extract_to_pdb step for Streamlit app.
    """
    job_main_output_folder = os.path.join(RESULTS_DIR, job_id)
    os.makedirs(job_main_output_folder, exist_ok=True)

    output_pdb_dir = os.path.join(job_main_output_folder, "pdbs")
    os.makedirs(output_pdb_dir, exist_ok=True)
    
    current_working_dir = POCKETHUNTER_DIR

    command = [
        'python', POCKETHUNTER_CLI,
        'extract_to_pdb',
        '--xtc', os.path.abspath(xtc_file_path),
        '--topology', os.path.abspath(topology_file_path),
        '--outfolder', os.path.abspath(output_pdb_dir), 
        '--stride', str(stride),
        '--overwrite'
    ]

    # Initial progress update
    self.update_state(
        state='PROGRESS', 
        meta={
            'current_step': 'Starting frame extraction',
            'progress': 5,
            'status': 'Initializing extraction process...',
            'elapsed': 0
        }
    )

    try:
        # Start the process
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
        update_interval = 0.5  # Update every 0.5 seconds for smoother progress
        progress_stages = [
            (0, 5, "Initializing..."),
            (2, 15, "Reading trajectory file..."),
            (5, 30, "Processing trajectory frames..."),
            (10, 50, "Converting frames to PDB format..."),
            (20, 70, "Writing PDB files..."),
            (30, 85, "Finalizing extraction..."),
            (60, 95, "Completing extraction...")
        ]
        
        # Monitor the process with real-time progress updates
        while process.poll() is None:
            time.sleep(0.5)  # Check more frequently
            current_time = time.time()
            elapsed = current_time - start_time
            
            # Determine progress based on elapsed time and stages
            current_stage = None
            for stage_elapsed, stage_progress, stage_desc in progress_stages:
                if elapsed >= stage_elapsed:
                    current_stage = (stage_progress, stage_desc)
            
            if current_stage:
                progress, stage_desc = current_stage
            else:
                # If we're past all stages, gradually increase to 95%
                if elapsed > 60:
                    progress = min(95, 85 + int(((elapsed - 60) / 30) * 10))
                else:
                    progress = 95
            
            # Send progress update more frequently
            if current_time - last_update >= update_interval:
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current_step': stage_desc,
                        'progress': progress,
                        'status': f'Frame extraction in progress... ({int(elapsed)}s elapsed)',
                        'elapsed': elapsed
                    }
                )
                last_update = current_time
        
        # Get final output
        stdout, stderr = process.communicate()
        elapsed = time.time() - start_time
        
        if process.returncode == 0:
            # Count extracted PDB files
            pdb_files = [f for f in os.listdir(output_pdb_dir) if f.endswith('.pdb')]
            
            # Final success update - keep at 100% for a moment before completing
            self.update_state(
                state='PROGRESS',
                meta={
                    'current_step': 'Frame extraction completed successfully!',
                    'progress': 100,
                    'status': f'Successfully extracted {len(pdb_files)} frames',
                    'elapsed': elapsed
                }
            )
            
            # Small delay to ensure UI sees the 100% progress
            time.sleep(1)
            
            results_overview = {
                'status': 'completed',
                'output_folder': job_main_output_folder, 
                'pdb_output_dir': output_pdb_dir,
                'frames_extracted': len(pdb_files),
                'processing_time': elapsed,
                'stdout': stdout,
                'stderr': stderr,
                'output_files': [os.path.join(output_pdb_dir, f) for f in pdb_files]
            }
            
            self.update_state(state='SUCCESS', meta=results_overview)
            return results_overview
        else:
            error_message = f"Frame extraction failed. Return code: {process.returncode}"
            self.update_state(
                state='FAILURE', 
                meta={
                    'status': error_message, 
                    'stdout': stdout, 
                    'stderr': stderr, 
                    'output_folder': job_main_output_folder
                }
            )
            raise Exception(f"{error_message}. Stderr: {stderr}")

    except subprocess.TimeoutExpired:
        error_message = "Frame extraction timed out after 10 minutes"
        self.update_state(
            state='FAILURE',
            meta={
                'status': error_message,
                'output_folder': job_main_output_folder
            }
        )
        raise Exception(error_message)
    except Exception as e:
        meta = {
            'status': f'Error occurred: {str(e)}', 
            'output_folder': job_main_output_folder,
            'exc_type': type(e).__name__,
            'exc_message': str(e)
        }
        self.update_state(state='FAILURE', meta=meta)
        raise


@celery_app.task(bind=True)
def run_detect_pockets_task(self, input_pdb_path_abs, job_id, numthreads):
    """
    PocketHunter detect_pockets step for Streamlit app.
    """
    self.update_state(
        state='PROGRESS', 
        meta={
            'current_step': 'Initializing pocket detection...',
            'progress': 0,
            'status': 'Pocket detection started'
        }
    )

    job_main_output_folder = os.path.join(RESULTS_DIR, job_id)
    os.makedirs(job_main_output_folder, exist_ok=True)

    output_pockets_dir = os.path.join(job_main_output_folder, "pockets")
    os.makedirs(output_pockets_dir, exist_ok=True)

    current_working_dir = POCKETHUNTER_DIR
    
    command = [
        'python', POCKETHUNTER_CLI,
        'detect_pockets',
        '--infolder', input_pdb_path_abs, 
        '--outfolder', os.path.abspath(output_pockets_dir), 
        '--numthreads', str(numthreads),
        '--compress',
        '--overwrite'
    ]
    
    self.update_state(
        state='PROGRESS', 
        meta={
            'current_step': 'Detecting pockets in PDB structures',
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
        update_interval = 3  # Update every 3 seconds
        
        # Monitor progress with real-time updates
        while process.poll() is None:
            time.sleep(1)
            current_time = time.time()
            elapsed = current_time - start_time
            
            # Update progress based on time elapsed (pocket detection can take longer)
            if elapsed < 15:  # First 15 seconds - initialization
                progress = min(15, 5 + int((elapsed / 15) * 10))
            elif elapsed < 60:  # Next 45 seconds - processing
                progress = min(50, 15 + int(((elapsed - 15) / 45) * 35))
            elif elapsed < 180:  # Next 2 minutes - more processing
                progress = min(80, 50 + int(((elapsed - 60) / 120) * 30))
            else:  # After 3 minutes - finishing up
                progress = min(95, 80 + int(((elapsed - 180) / 60) * 15))
            
            # Send progress update every few seconds
            if current_time - last_update >= update_interval:
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current_step': f'Analyzing protein structures (elapsed: {int(elapsed)}s)',
                        'progress': progress,
                        'status': f'Pocket detection in progress... ({int(elapsed)}s elapsed)',
                        'elapsed': elapsed
                    }
                )
                last_update = current_time
        
        stdout, stderr = process.communicate()
        elapsed = time.time() - start_time

        if process.returncode == 0:
            # Find pockets.csv file
            pockets_csv_abs = os.path.join(output_pockets_dir, 'pockets.csv')
            
            # Count detected pockets
            pockets_detected = 0
            if os.path.exists(pockets_csv_abs):
                import pandas as pd
                try:
                    df = pd.read_csv(pockets_csv_abs)
                    pockets_detected = len(df)
                except:
                    pockets_detected = 0
            
            # Final success update
            self.update_state(
                state='PROGRESS',
                meta={
                    'current_step': 'Pocket detection completed',
                    'progress': 100,
                    'status': f'Successfully detected {pockets_detected} pockets',
                    'elapsed': elapsed
                }
            )
            
            results_overview = {
                'status': 'completed',
                'pockets_output_dir_abs': output_pockets_dir,
                'pockets_csv_abs': pockets_csv_abs if os.path.exists(pockets_csv_abs) else None,
                'pockets_detected': pockets_detected,
                'processing_time': elapsed,
                'stdout': stdout,
                'stderr': stderr
            }
            
            self.update_state(state='SUCCESS', meta=results_overview)
            return results_overview
        else:
            error_message = f"Pocket detection failed. Return code: {process.returncode}"
            self.update_state(
                state='FAILURE', 
                meta={
                    'status': error_message, 
                    'stdout': stdout, 
                    'stderr': stderr, 
                    'output_folder': job_main_output_folder
                }
            )
            raise Exception(f"{error_message}. Stderr: {stderr}")

    except Exception as e:
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
        
        # Monitor progress with real-time updates
        while process.poll() is None:
            time.sleep(1)
            current_time = time.time()
            elapsed = current_time - start_time
            
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
        
        stdout, stderr = process.communicate()
        elapsed = time.time() - start_time

        if process.returncode == 0:
            # Find output files (check for both possible naming conventions)
            clustered_pockets_csv_abs = os.path.join(output_clusters_dir, 'pockets_clustered.csv')
            if not os.path.exists(clustered_pockets_csv_abs):
                clustered_pockets_csv_abs = os.path.join(output_clusters_dir, 'clustered_pockets.csv')
            
            representatives_csv_abs = os.path.join(output_clusters_dir, 'cluster_representatives.csv')
            if not os.path.exists(representatives_csv_abs):
                representatives_csv_abs = os.path.join(output_clusters_dir, 'representatives.csv')
            
            # Debug: List all files in the output directory
            import glob
            all_files = glob.glob(os.path.join(output_clusters_dir, '*.csv'))
            print(f"Debug: Found CSV files in {output_clusters_dir}: {all_files}")
            print(f"Debug: Looking for clustered_pockets_csv_abs: {clustered_pockets_csv_abs}")
            print(f"Debug: Looking for representatives_csv_abs: {representatives_csv_abs}")
            print(f"Debug: clustered_pockets_csv_abs exists: {os.path.exists(clustered_pockets_csv_abs)}")
            print(f"Debug: representatives_csv_abs exists: {os.path.exists(representatives_csv_abs)}")
            
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
                    print(f"Debug: Error reading clustered_pockets_csv: {e}")
                    pass
            
            if os.path.exists(representatives_csv_abs):
                try:
                    df = pd.read_csv(representatives_csv_abs)
                    representatives = len(df)
                except Exception as e:
                    print(f"Debug: Error reading representatives_csv: {e}")
                    pass
            
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
            
            # Update the status file to reflect completion
            status_file = os.path.join(RESULTS_DIR, f'{job_id}_status.json')
            completion_status = {
                'status': 'completed',
                'step': 'Pocket clustering completed successfully',
                'last_updated': datetime.now().isoformat(),
                'task_id': self.request.id,
                'result_info': results_overview
            }
            with open(status_file, 'w') as f:
                json.dump(completion_status, f, indent=4)
            
            self.update_state(state='SUCCESS', meta=results_overview)
            return results_overview
        else:
            error_message = f"Pocket clustering failed. Return code: {process.returncode}"
            meta = {
                'status': error_message, 
                'stdout': stdout, 
                'stderr': stderr, 
                'output_folder': job_main_output_folder,
                'exc_type': 'Exception',
                'exc_message': f"{error_message}. Stderr: {stderr}"
            }
            
            # Update the status file to reflect failure
            status_file = os.path.join(RESULTS_DIR, f'{job_id}_status.json')
            failure_status = {
                'status': 'failed',
                'step': 'Pocket clustering failed',
                'last_updated': datetime.now().isoformat(),
                'task_id': self.request.id,
                'error_info': meta
            }
            with open(status_file, 'w') as f:
                json.dump(failure_status, f, indent=4)
            
            self.update_state(state='FAILURE', meta=meta)
            raise Exception(f"{error_message}. Stderr: {stderr}")

    except Exception as e:
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


@celery_app.task(bind=True)
def run_docking_task(self, cluster_representatives_csv, ligand_folder, job_id, smina_exe_path=None, num_poses=10, exhaustiveness=8, ph_value=7.4, box_size_x=20.0, box_size_y=20.0, box_size_z=20.0):
    """
    Molecular docking task for Streamlit app.
    """
    start_time = time.time()
    
    self.update_state(
        state='PROGRESS', 
        meta={
            'current_step': 'Initializing docking...',
            'progress': 0,
            'status': 'Docking started'
        }
    )

    # Create output directory
    output_folder_job = os.path.join(RESULTS_DIR, f'dock_{job_id}')
    os.makedirs(output_folder_job, exist_ok=True)
    
    # Set default smina path if not provided
    if smina_exe_path is None:
        smina_exe_path = 'smina'  # Assume smina is in PATH
    
    self.update_state(
        state='PROGRESS', 
        meta={
            'current_step': 'Reading cluster representatives',
            'progress': 10,
            'status': 'Loading pocket data...'
        }
    )

    try:
        # Import docking functions
        import sys
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        from step4_docking import dock_ensemble, pdb_to_pdbqt, calc_box, run_smina, parse_smina_log
        
        # Read cluster representatives
        df_rep_pockets = pd.read_csv(cluster_representatives_csv)
        
        self.update_state(
            state='PROGRESS', 
            meta={
                'current_step': 'Preparing docking calculations',
                'progress': 20,
                'status': f'Found {len(df_rep_pockets)} cluster representatives'
            }
        )
        
        # Run docking ensemble
        df_outputs = dock_ensemble(
            df_rep_pockets=df_rep_pockets,
            ligand_folder=ligand_folder,
            smina_exe=smina_exe_path,
            out_folder=output_folder_job,
            num_poses=num_poses,
            exhaustiveness=exhaustiveness,
            ph_value=ph_value,
            box_size_x=box_size_x,
            box_size_y=box_size_y,
            box_size_z=box_size_z
        )
        
        # Save results
        docking_results_file = os.path.join(output_folder_job, 'docking_results.csv')
        df_outputs.to_csv(docking_results_file, index=False)
        
        elapsed = time.time() - start_time
        
        # Calculate statistics
        total_docking_poses = len(df_outputs)
        unique_ligands = df_outputs['ligand'].nunique()
        unique_receptors = df_outputs['receptor'].nunique()
        best_affinity = df_outputs['affinity (kcal/mol)'].min()
        
        self.update_state(
            state='PROGRESS',
            meta={
                'current_step': 'Docking completed',
                'progress': 100,
                'status': f'Successfully docked {unique_ligands} ligands to {unique_receptors} receptors',
                'elapsed': elapsed
            }
        )
        
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
            'exhaustiveness': exhaustiveness
        }
        
        # Update the status file to reflect completion
        status_file = os.path.join(RESULTS_DIR, f'dock_{job_id}_status.json')
        completion_status = {
            'status': 'completed',
            'step': 'Molecular docking completed successfully',
            'last_updated': datetime.now().isoformat(),
            'task_id': self.request.id,
            'result_info': results_overview
        }
        with open(status_file, 'w') as f:
            json.dump(completion_status, f, indent=4)
        
        self.update_state(state='SUCCESS', meta=results_overview)
        return results_overview
        
    except Exception as e:
        elapsed = time.time() - start_time
        meta = {
            'status': f'Error occurred: {str(e)}', 
            'output_folder': output_folder_job,
            'exc_type': type(e).__name__,
            'exc_message': str(e),
            'processing_time': elapsed
        }
        
        # Update the status file to reflect failure
        status_file = os.path.join(RESULTS_DIR, f'dock_{job_id}_status.json')
        failure_status = {
            'status': 'failed',
            'step': 'Molecular docking failed',
            'last_updated': datetime.now().isoformat(),
            'task_id': self.request.id,
            'error_info': meta
        }
        with open(status_file, 'w') as f:
            json.dump(failure_status, f, indent=4)
        
        self.update_state(state='FAILURE', meta=meta)
        raise 