import streamlit as st
import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import time
import json
import uuid
from tasks import run_cluster_pockets_task
from celery_app import celery_app
from config import Config
from security import handle_file_upload_secure, SecurityError
from logging_config import setup_logging
import py3Dmol
import streamlit.components.v1 as components
from pathlib import Path

# Use Config for directories
UPLOAD_DIR = str(Config.UPLOAD_DIR)
RESULTS_DIR = str(Config.RESULTS_DIR)

# Setup logging
logger = setup_logging(__name__)

# Custom CSS for enhanced UI
st.markdown("""
<style>
    .cluster-header {
        background: linear-gradient(135deg, #FFA726 0%, #FB8C00 50%, #EF6C00 100%);
        padding: 2rem;
        border-radius: 20px;
        margin-bottom: 2rem;
        color: white;
        text-align: center;
        box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        border: 1px solid rgba(255, 255, 255, 0.18);
    }

    .cluster-header h1 {
        margin: 0;
        font-size: 2.5rem;
        font-weight: 700;
        text-shadow: 2px 2px 4px rgba(0,0,0,0.1);
    }

    .cluster-card {
        background: rgba(255, 255, 255, 0.95);
        backdrop-filter: blur(10px);
        padding: 2rem;
        border-radius: 15px;
        box-shadow: 0 8px 32px rgba(31, 38, 135, 0.15);
        border: 1px solid rgba(255, 255, 255, 0.18);
        margin: 1.5rem 0;
        transition: all 0.3s ease;
    }

    .cluster-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 12px 48px rgba(31, 38, 135, 0.2);
    }

    .metric-card {
        background: linear-gradient(135deg, #FFF3E0 0%, #FFE0B2 100%);
        padding: 1.5rem;
        border-radius: 15px;
        text-align: center;
        box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        transition: all 0.3s ease;
    }

    .metric-card:hover {
        transform: translateY(-5px);
        box-shadow: 0 8px 25px rgba(0,0,0,0.15);
    }

    .metric-value {
        font-size: 2rem;
        font-weight: 700;
        color: #F57C00;
        margin: 0.5rem 0;
    }

    .metric-label {
        font-size: 0.9rem;
        color: #666;
        text-transform: uppercase;
        letter-spacing: 1px;
    }

    .job-id-display {
        background: linear-gradient(135deg, #FFA726 0%, #FB8C00 100%);
        color: white;
        padding: 1.2rem;
        border-radius: 15px;
        font-family: 'Courier New', monospace;
        font-size: 1.1rem;
        text-align: center;
        margin: 1.5rem 0;
        box-shadow: 0 4px 20px rgba(255, 167, 38, 0.3);
        border: 1px solid rgba(255, 255, 255, 0.2);
    }

    .cluster-badge {
        display: inline-block;
        padding: 0.4rem 1rem;
        border-radius: 25px;
        font-size: 0.85rem;
        font-weight: 600;
        text-align: center;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        background: linear-gradient(135deg, #FFA726 0%, #FB8C00 100%);
        color: white;
    }

    /* Tabs styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }

    .stTabs [data-baseweb="tab"] {
        height: 50px;
        background-color: #FFF3E0;
        border-radius: 10px;
        padding: 0 24px;
        font-weight: 600;
    }

    .stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, #FFA726 0%, #FB8C00 100%);
        color: white;
    }
</style>
""", unsafe_allow_html=True)

# Header
st.markdown("""
<div class="cluster-header">
    <h1>🎯 Step 3: Pocket Clustering</h1>
    <p style="font-size: 1.2rem; margin-top: 0.5rem;">Group similar pockets to identify representative binding sites</p>
</div>
""", unsafe_allow_html=True)

# Session state initialization
if 'cluster_job_id' not in st.session_state:
    st.session_state.cluster_job_id = None
if 'cluster_task_id' not in st.session_state:
    st.session_state.cluster_task_id = None
if 'cluster_status' not in st.session_state:
    st.session_state.cluster_status = 'idle'

# Initialize cached_job_ids if not exists
if 'cached_job_ids' not in st.session_state:
    st.session_state.cached_job_ids = {}

# Helper functions
def update_job_status(job_id, status, step=None, task_id=None, result_info=None):
    status_file = os.path.join(RESULTS_DIR, f'{job_id}_status.json')
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
    current_status['last_updated'] = datetime.now().isoformat()

    with open(status_file, 'w') as f:
        json.dump(current_status, f, indent=4)

def show_molecule_3d(pdb_path, width=800, height=600, style="cartoon"):
    """Display 3D molecular structure using py3Dmol"""
    try:
        with open(pdb_path, 'r') as f:
            pdb_data = f.read()

        view = py3Dmol.view(width=width, height=height)
        view.addModel(pdb_data, 'pdb')

        if style == "cartoon":
            view.setStyle({'cartoon': {'color': 'spectrum'}})
        elif style == "surface":
            view.setStyle({'surface': {'opacity': 0.7, 'color': 'spectrum'}})
        elif style == "stick":
            view.setStyle({'stick': {'colorscheme': 'spectrum'}})

        view.zoomTo()
        view.spin(False)

        html = f"""
        <div style="border-radius: 15px; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.1);">
            {view._make_html()}
        </div>
        """
        components.html(html, height=height+50, scrolling=False)
    except Exception as e:
        st.error(f"Error loading 3D structure: {e}")
        logger.error(f"Error in show_molecule_3d: {e}", exc_info=True)

# Create tabs for different views
tab_setup, tab_progress, tab_results = st.tabs(["🚀 Setup & Launch", "📊 Progress", "🎯 Results & Visualization"])

with tab_setup:
    st.markdown("### 📁 Input Configuration")

    # Display current job ID if exists
    if st.session_state.cluster_job_id:
        st.markdown(f"""
        <div class="job-id-display">
            🔑 Current Job ID: {st.session_state.cluster_job_id}
        </div>
        """, unsafe_allow_html=True)

    # Input options in columns
    st.markdown("#### Select Input Source")

    input_col1, input_col2 = st.columns(2)

    with input_col1:
        st.markdown("**Option 1: Use Previous Step**")
        cached_detect_id = st.session_state.cached_job_ids.get('detect', '')
        detect_job_id = st.text_input(
            "Job ID from Step 2:",
            value=cached_detect_id,
            key="cluster_detect_job_id",
            help="Enter the Job ID from pocket detection"
        )

    with input_col2:
        st.markdown("**Option 2: Upload CSV**")
        pockets_csv = st.file_uploader(
            "Upload pockets.csv:",
            type=['csv'],
            key="cluster_pockets_csv",
            help="Upload pockets.csv from pocket detection"
        )

    st.markdown("#### ⚙️ Clustering Parameters")

    param_col1, param_col2 = st.columns(2)

    with param_col1:
        min_prob = st.slider(
            "Min. Ligand-Binding Probability",
            min_value=0.0,
            max_value=1.0,
            value=0.5,
            step=0.05,
            key="cluster_min_prob",
            help="Minimum probability threshold for pocket clustering"
        )

    with param_col2:
        clustering_method = st.selectbox(
            "Clustering Method",
            options=["dbscan", "hierarchical"],
            index=0,
            key="cluster_method",
            help="DBSCAN: Density-based | Hierarchical: Tree-based"
        )

    # Advanced options
    if clustering_method == "dbscan":
        dbscan_hierarchical = st.checkbox(
            "Enable Hierarchical Refinement",
            value=True,
            key="cluster_dbscan_hierarchical",
            help="Apply hierarchical sub-clustering within DBSCAN clusters"
        )
    else:
        dbscan_hierarchical = False

    # Run button
    st.markdown("---")
    if st.button("🚀 Start Pocket Clustering", type="primary", use_container_width=True):
        # Determine input source
        pockets_csv_path = None
        input_source = None

        if detect_job_id:
            detect_output_dir = os.path.join(RESULTS_DIR, detect_job_id, "pockets")
            potential_csv_path = os.path.join(detect_output_dir, "pockets.csv")
            if os.path.exists(potential_csv_path):
                pockets_csv_path = potential_csv_path
                input_source = f"Step 2 results (Job ID: {detect_job_id})"
            else:
                st.error(f"pockets.csv not found for Job ID: {detect_job_id}")
                st.stop()

        elif pockets_csv:
            job_id = f"cluster_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            try:
                csv_path = handle_file_upload_secure(pockets_csv, job_id, "pockets_")
                logger.info(f"CSV file uploaded for job {job_id}")
            except SecurityError as e:
                st.error(f"❌ File upload failed: {e}")
                logger.error(f"Security error during CSV upload: {e}")
                st.stop()
            if csv_path:
                pockets_csv_path = csv_path
                input_source = "Uploaded pockets.csv"

        else:
            st.error("Please provide input using one of the options above.")
            st.stop()

        if pockets_csv_path:
            # Generate unique job ID
            job_id = f"cluster_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            st.session_state.cluster_job_id = job_id

            # Update status
            update_job_status(job_id, 'submitted', 'Initializing pocket clustering')
            st.session_state.cluster_status = 'running'

            # Start the clustering
            with st.spinner("Starting pocket clustering..."):
                task = run_cluster_pockets_task.delay(
                    pockets_csv_path_abs=os.path.abspath(pockets_csv_path),
                    job_id=job_id,
                    min_prob=min_prob,
                    clustering_method=clustering_method,
                    dbscan_hierarchical=dbscan_hierarchical
                )
                st.session_state.cluster_task_id = task.id
                update_job_status(job_id, 'running', 'Pocket clustering started', task_id=task.id)

            st.success(f"✅ Clustering started! Job ID: `{job_id}`")
            st.info(f"📂 Input: {input_source}")
            st.info("💡 Switch to the Progress tab to monitor execution")
            time.sleep(2)
            st.rerun()

with tab_progress:
    st.markdown("### 📊 Clustering Progress")

    if st.session_state.cluster_task_id:
        try:
            task = celery_app.AsyncResult(st.session_state.cluster_task_id)

            if task.state == 'PENDING':
                st.info("⏳ Task is pending in queue...")
                st.progress(0)
                if st.button("🔄 Refresh"):
                    st.rerun()

            elif task.state == 'PROGRESS':
                progress_info = task.info or {}
                progress = progress_info.get('progress', 0)
                current_step = progress_info.get('current_step', 'Processing...')
                status_msg = progress_info.get('status', 'Running...')

                st.info(f"🔄 {current_step}")
                st.progress(progress / 100)

                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Progress", f"{progress:.1f}%")
                with col2:
                    st.metric("Status", status_msg)
                with col3:
                    if 'elapsed' in progress_info:
                        st.metric("Elapsed", f"{progress_info['elapsed']:.1f}s")

                time.sleep(2)
                st.rerun()

            elif task.state == 'SUCCESS':
                st.success("✅ Clustering completed successfully!")
                st.progress(1.0)

                result = task.result
                if result:
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        st.metric("Clusters Found", result.get('clusters_found', 'N/A'))
                    with col2:
                        st.metric("Processing Time", f"{result.get('processing_time', 0):.1f}s")
                    with col3:
                        st.metric("Status", "Complete")

                st.session_state.cluster_status = 'completed'
                st.session_state.cached_job_ids['cluster'] = st.session_state.cluster_job_id
                st.info("💡 Switch to the Results & Visualization tab to explore your clusters!")

            elif task.state == 'FAILURE':
                st.error("❌ Clustering failed!")
                error_msg = str(task.info) if task.info else 'Unknown error'
                st.error(f"Error: {error_msg}")

        except Exception as e:
            st.error(f"Error checking task status: {e}")
            logger.error(f"Task status error: {e}", exc_info=True)
    else:
        st.info("ℹ️ No active clustering job. Start a new job in the Setup & Launch tab.")

with tab_results:
    st.markdown("### 🎯 Clustering Results")

    # Option to load existing results
    col1, col2 = st.columns([3, 1])
    with col1:
        load_job_id = st.text_input(
            "Enter Clustering Job ID:",
            value=st.session_state.cluster_job_id if st.session_state.cluster_job_id else "",
            placeholder="e.g., cluster_20250815_143022_a1b2c3d4",
            help="Enter a clustering job ID to view results"
        )
    with col2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🔍 Load Results", use_container_width=True):
            if load_job_id:
                st.session_state.cluster_job_id = load_job_id
                st.rerun()

    if st.session_state.cluster_job_id:
        # Check if results exist
        cluster_output_dir = os.path.join(RESULTS_DIR, st.session_state.cluster_job_id, "pocket_clusters")
        representatives_file = os.path.join(cluster_output_dir, "cluster_representatives.csv")

        if os.path.exists(representatives_file):
            try:
                df_reps = pd.read_csv(representatives_file)

                # Display overview metrics
                st.markdown("#### 📊 Clustering Overview")

                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Total Clusters</div>
                        <div class="metric-value">{len(df_reps)}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col2:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Avg Probability</div>
                        <div class="metric-value">{df_reps['probability'].mean():.3f}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col3:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Avg Residues</div>
                        <div class="metric-value">{df_reps['residues'].mean():.0f}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col4:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Best Probability</div>
                        <div class="metric-value">{df_reps['probability'].max():.3f}</div>
                    </div>
                    """, unsafe_allow_html=True)

                # Create sub-tabs for different analyses
                results_tab1, results_tab2, results_tab3, results_tab4 = st.tabs([
                    "📋 Cluster Table",
                    "📈 Distribution Analysis",
                    "🔬 3D Viewer",
                    "💾 Download"
                ])

                with results_tab1:
                    st.markdown("#### 🏆 Cluster Representatives")

                    # Sort and display
                    df_display = df_reps.sort_values('probability', ascending=False)

                    # Add quality badges
                    def get_quality_badge(prob):
                        if prob >= 0.8:
                            return "🟢 Excellent"
                        elif prob >= 0.6:
                            return "🟡 Good"
                        elif prob >= 0.4:
                            return "🟠 Moderate"
                        else:
                            return "🔴 Low"

                    df_display['Quality'] = df_display['probability'].apply(get_quality_badge)

                    st.dataframe(
                        df_display[['File name', 'probability', 'residues', 'Quality']],
                        use_container_width=True,
                        height=400
                    )

                    # Selection for 3D viewing
                    st.markdown("---")
                    st.markdown("**Select a cluster to view in 3D:**")
                    selected_idx = st.selectbox(
                        "Choose cluster:",
                        df_display.index,
                        format_func=lambda x: f"{df_display.loc[x, 'File name']} (Prob: {df_display.loc[x, 'probability']:.3f})"
                    )
                    if selected_idx is not None:
                        st.session_state.selected_cluster = df_display.loc[selected_idx].to_dict()
                        st.info("✅ Cluster selected! Switch to the 3D Viewer tab to visualize it.")

                with results_tab2:
                    st.markdown("#### 📈 Statistical Analysis")

                    col1, col2 = st.columns(2)

                    with col1:
                        # Probability distribution
                        fig_hist = px.histogram(
                            df_reps,
                            x='probability',
                            title='Probability Distribution',
                            nbins=20,
                            color_discrete_sequence=['#FFA726']
                        )
                        fig_hist.update_layout(
                            xaxis_title="Binding Probability",
                            yaxis_title="Number of Clusters",
                            showlegend=False
                        )
                        st.plotly_chart(fig_hist, use_container_width=True)

                    with col2:
                        # Residue count distribution
                        fig_residues = px.box(
                            df_reps,
                            y='residues',
                            title='Residue Count Distribution',
                            color_discrete_sequence=['#FB8C00']
                        )
                        fig_residues.update_layout(
                            yaxis_title="Number of Residues",
                            showlegend=False
                        )
                        st.plotly_chart(fig_residues, use_container_width=True)

                    # Scatter plot
                    st.markdown("#### 🎯 Probability vs Size")
                    fig_scatter = px.scatter(
                        df_reps,
                        x='residues',
                        y='probability',
                        size='probability',
                        color='probability',
                        title='Cluster Probability vs Pocket Size',
                        labels={'residues': 'Number of Residues', 'probability': 'Binding Probability'},
                        color_continuous_scale='Oranges',
                        hover_data=['File name']
                    )
                    fig_scatter.update_traces(marker=dict(line=dict(width=1, color='DarkOrange')))
                    st.plotly_chart(fig_scatter, use_container_width=True)

                    # Statistics
                    st.markdown("#### 📊 Statistical Summary")
                    stats_col1, stats_col2, stats_col3, stats_col4 = st.columns(4)
                    with stats_col1:
                        st.metric("Mean Probability", f"{df_reps['probability'].mean():.3f}")
                    with stats_col2:
                        st.metric("Median Probability", f"{df_reps['probability'].median():.3f}")
                    with stats_col3:
                        st.metric("Std Dev", f"{df_reps['probability'].std():.3f}")
                    with stats_col4:
                        high_quality = len(df_reps[df_reps['probability'] >= 0.7])
                        st.metric("High Quality (≥0.7)", high_quality)

                with results_tab3:
                    st.markdown("### 🔬 3D Cluster Viewer")

                    if 'selected_cluster' in st.session_state and st.session_state.selected_cluster:
                        cluster = st.session_state.selected_cluster

                        st.markdown(f"""
                        <div class="cluster-card">
                            <h4>🎯 Selected Cluster</h4>
                            <p><strong>File:</strong> {cluster.get('File name', 'N/A')}</p>
                            <p><strong>Probability:</strong> <span class="cluster-badge">
                                {cluster.get('probability', 0):.3f}
                            </span></p>
                            <p><strong>Residues:</strong> {cluster.get('residues', 0)}</p>
                        </div>
                        """, unsafe_allow_html=True)

                        # Visualization controls
                        col1, col2 = st.columns(2)
                        with col1:
                            viz_style = st.selectbox(
                                "Visualization Style:",
                                ["cartoon", "surface", "stick"],
                                help="Choose how to display the pocket structure"
                            )
                        with col2:
                            st.markdown("<br>", unsafe_allow_html=True)

                        # Load and display the structure
                        pdb_filename = cluster.get('File name')
                        if pdb_filename:
                            pdb_path = os.path.join(cluster_output_dir, pdb_filename)
                            if os.path.exists(pdb_path):
                                show_molecule_3d(pdb_path, style=viz_style)
                            else:
                                st.warning(f"⚠️ PDB file not found: {pdb_path}")
                        else:
                            st.error("No PDB file information in cluster data")

                    else:
                        st.info("ℹ️ No cluster selected. Go to the Cluster Table tab and select a cluster to view.")

                        # Demo: show first available cluster
                        if len(df_reps) > 0:
                            st.markdown("#### 📺 Preview: First Cluster")
                            first_cluster = df_reps.iloc[0]
                            pdb_filename = first_cluster['File name']
                            pdb_path = os.path.join(cluster_output_dir, pdb_filename)
                            if os.path.exists(pdb_path):
                                show_molecule_3d(pdb_path, width=600, height=400)

                with results_tab4:
                    st.markdown("#### 💾 Download Results")

                    col1, col2 = st.columns(2)

                    with col1:
                        st.markdown("**📄 Data Files**")

                        # CSV download
                        csv_data = df_reps.to_csv(index=False)
                        st.download_button(
                            label="📥 Download Cluster Representatives (CSV)",
                            data=csv_data,
                            file_name=f"cluster_representatives_{st.session_state.cluster_job_id}.csv",
                            mime="text/csv",
                            use_container_width=True
                        )

                        # High quality clusters only
                        df_high_quality = df_reps[df_reps['probability'] >= 0.7]
                        if len(df_high_quality) > 0:
                            hq_csv = df_high_quality.to_csv(index=False)
                            st.download_button(
                                label="📥 Download High Quality Clusters (CSV)",
                                data=hq_csv,
                                file_name=f"high_quality_clusters_{st.session_state.cluster_job_id}.csv",
                                mime="text/csv",
                                use_container_width=True
                            )

                    with col2:
                        st.markdown("**📦 Structure Files**")

                        # Create ZIP with PDB files
                        if st.button("🔄 Generate PDB Archive", use_container_width=True):
                            import zipfile
                            with st.spinner("Creating archive..."):
                                zip_path = os.path.join(cluster_output_dir, 'cluster_structures.zip')
                                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                                    for pdb_file in Path(cluster_output_dir).glob('*.pdb'):
                                        zipf.write(pdb_file, pdb_file.name)
                                st.success("✅ Archive created!")

                        # Download ZIP
                        zip_path = os.path.join(cluster_output_dir, 'cluster_structures.zip')
                        if os.path.exists(zip_path):
                            with open(zip_path, 'rb') as f:
                                st.download_button(
                                    label="📥 Download All PDB Files (ZIP)",
                                    data=f.read(),
                                    file_name=f"cluster_structures_{st.session_state.cluster_job_id}.zip",
                                    mime="application/zip",
                                    use_container_width=True
                                )

                    st.markdown("---")
                    st.info("💡 Use the cluster representatives CSV in Step 4: Molecular Docking")

            except Exception as e:
                st.error(f"Error loading results: {e}")
                logger.error(f"Results loading error: {e}", exc_info=True)
        else:
            st.info("ℹ️ No results found for this job ID. Make sure clustering has completed successfully.")
    else:
        st.info("ℹ️ No job selected. Start a new job or enter a job ID above.")

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666;'>
    <p>🎯 Pocket Clustering | Part of the PocketHunter Suite</p>
    <p style='font-size: 0.85rem; margin-top: 0.5rem;'>
        💡 Tip: High-probability clusters (≥0.7) are recommended for docking studies
    </p>
</div>
""", unsafe_allow_html=True)
