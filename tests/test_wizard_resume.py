"""
Tests for _detect_stage_from_disk — the only pure logic in wizard_app.py.
Imports the function directly after temporarily monkey-patching streamlit.
"""
import json
import os
import sys
import types
import pytest
from pathlib import Path

# ── Stub streamlit so wizard_app can be imported outside Streamlit runtime ──
class SessionState(dict):
    """Dict that allows attribute access."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(f"No attribute {key}")
    def __setattr__(self, key, value):
        self[key] = value

class FakeColumn:
    def write(self, *a, **kw): pass
    def markdown(self, *a, **kw): pass
    def button(self, *a, **kw): return False
    def text_input(self, *a, **kw): return ""
    def selectbox(self, *a, **kw): return None
    def file_uploader(self, *a, **kw): return None
    def slider(self, *a, **kw): return 0
    def text_area(self, *a, **kw): return ""
    def __enter__(self): return self
    def __exit__(self, *a): pass

st_stub = types.ModuleType('streamlit')
st_stub.session_state = SessionState({'wiz_stage': 'setup'})
st_stub.cache_data = lambda **kw: (lambda f: f)
st_stub.columns = lambda *a, **kw: [FakeColumn(), FakeColumn()]
st_stub.tabs = lambda *a, **kw: [FakeColumn()]
st_stub.expander = lambda *a, **kw: FakeColumn()

for attr in ('markdown','write','error','warning','info','success','button',
             'file_uploader','slider','selectbox','text_input',
             'progress','rerun','stop','code','download_button',
             'dataframe','plotly_chart','caption','subheader'):
    setattr(st_stub, attr, lambda *a, **kw: None)
sys.modules['streamlit'] = st_stub
sys.modules['streamlit.components'] = types.ModuleType('streamlit.components')
sys.modules['streamlit.components.v1'] = types.ModuleType('streamlit.components.v1')

# Stub heavy deps
for mod in ('tasks','celery_app','config','session_state','security',
            'rate_limiter','logging_config','py3Dmol','plotly',
            'plotly.graph_objects','pandas'):
    if mod not in sys.modules:
        m = types.ModuleType(mod)
        if mod == 'config':
            class _Config:
                RESULTS_DIR = '/tmp/ph_test_results'
                UPLOAD_DIR  = '/tmp/ph_test_uploads'
            m.Config = _Config
        elif mod == 'tasks':
            m.run_pockethunter_pipeline = lambda *a, **kw: None
            m.run_discrimination_task = lambda *a, **kw: None
        elif mod == 'celery_app':
            class _CeleryApp:
                def control(self):
                    class C:
                        def inspect(self):
                            return {}
                    return C()
            m.celery_app = _CeleryApp()
        elif mod == 'logging_config':
            import logging
            m.setup_logging = lambda name: logging.getLogger(name)
        elif mod == 'pandas':
            import pandas as pd
            sys.modules['pandas'] = pd
            continue
        elif mod == 'security':
            m.handle_file_upload_secure = lambda *a, **kw: None
            m.SecurityError = Exception
        elif mod == 'rate_limiter':
            m.check_task_rate_limit = lambda *a, **kw: None
            m.RateLimitExceeded = Exception
        elif mod == 'session_state':
            m.initialize_session_state = lambda: None
        sys.modules[mod] = m

# Now import the function under test
sys.path.insert(0, str(Path(__file__).parent.parent))
from wizard_app import _detect_stage_from_disk


RESULTS_DIR = '/tmp/ph_test_results'


@pytest.fixture(autouse=True)
def clean_results(tmp_path, monkeypatch):
    monkeypatch.setattr('wizard_app.RESULTS_DIR', str(tmp_path))
    return tmp_path


def _write_status(tmp_path, job_id, data):
    (tmp_path / f"{job_id}_status.json").write_text(json.dumps(data))


def _make_reps_csv(tmp_path, job_id):
    pocket_dir = tmp_path / job_id / 'pocket_clusters'
    pocket_dir.mkdir(parents=True, exist_ok=True)
    (pocket_dir / 'cluster_representatives.csv').write_text('cluster,File name,residues\n0,frame_001.pdb,A_10\n')


def _make_disc_csv(tmp_path, disc_job_id):
    disc_dir = tmp_path / disc_job_id / 'discrimination'
    disc_dir.mkdir(parents=True, exist_ok=True)
    csv_path = disc_dir / 'discrimination_results.csv'
    csv_path.write_text('cluster_id,frame,roc_auc\n0,1,0.75\n')
    return str(csv_path)


def test_invalid_job_id_returns_not_valid(tmp_path):
    result = _detect_stage_from_disk('nonexistent_job', str(tmp_path))
    assert result['valid'] is False


def test_pipeline_running_stage(tmp_path):
    job_id = 'pipeline_20240408_aabb'
    _write_status(tmp_path, job_id, {
        'status': 'running',
        'task_id': 'celery-task-abc',
    })
    result = _detect_stage_from_disk(job_id, str(tmp_path))
    assert result['valid'] is True
    assert result['stage'] == 'pipeline_running'
    assert result['pipeline_task_id'] == 'celery-task-abc'


def test_pipeline_done_stage(tmp_path):
    job_id = 'pipeline_20240408_ccdd'
    _make_reps_csv(tmp_path, job_id)
    _write_status(tmp_path, job_id, {
        'status': 'completed',
        'task_id': 'celery-task-xyz',
        'result_info': {'frames_extracted': 142, 'representatives': 7},
    })
    result = _detect_stage_from_disk(job_id, str(tmp_path))
    assert result['valid'] is True
    assert result['stage'] == 'pipeline_done'
    assert result['pipeline_result']['frames_extracted'] == 142


def test_disc_running_stage(tmp_path):
    job_id = 'pipeline_20240408_eeff'
    disc_id = 'disc_11223344'
    _make_reps_csv(tmp_path, job_id)
    _write_status(tmp_path, job_id, {
        'status': 'completed',
        'task_id': 'celery-pipeline',
        'result_info': {},
        'disc_job_id': disc_id,
        'disc_task_id': 'celery-disc',
    })
    _write_status(tmp_path, disc_id, {'status': 'running', 'task_id': 'celery-disc'})
    result = _detect_stage_from_disk(job_id, str(tmp_path))
    assert result['stage'] == 'disc_running'
    assert result['disc_job_id'] == disc_id
    assert result['disc_task_id'] == 'celery-disc'


def test_complete_stage(tmp_path):
    job_id = 'pipeline_20240408_gghh'
    disc_id = 'disc_55667788'
    csv_path = _make_disc_csv(tmp_path, disc_id)
    _make_reps_csv(tmp_path, job_id)
    _write_status(tmp_path, job_id, {
        'status': 'completed',
        'task_id': 'celery-pipeline',
        'result_info': {},
        'disc_job_id': disc_id,
        'disc_task_id': 'celery-disc',
    })
    _write_status(tmp_path, disc_id, {
        'status': 'completed',
        'result_info': {'discrimination_results_csv': csv_path},
    })
    result = _detect_stage_from_disk(job_id, str(tmp_path))
    assert result['stage'] == 'complete'
    assert result['disc_job_id'] == disc_id
