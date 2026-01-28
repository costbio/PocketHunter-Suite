# PocketHunter Suite

A web-based interface for molecular dynamics pocket detection, clustering, and docking analysis. Built with Streamlit for an interactive experience with real-time task monitoring and 3D visualization.

## Features

- **Frame Extraction**: Convert MD trajectories (XTC/TRR) to individual PDB snapshots
- **Pocket Detection**: Identify binding sites across trajectory frames using PocketHunter
- **Pocket Clustering**: Group similar pockets and select representative conformations
- **Molecular Docking**: Dock ligands to pocket representatives using SMINA
- **3D Visualization**: Interactive molecular viewer with pocket highlighting
- **Task Monitoring**: Real-time progress tracking with status history

## Quick Start with Docker

The recommended way to run PocketHunter Suite is with Docker Compose.

```bash
# Clone the repository
git clone git@github.com:bogrum/PocketHunter-Suite.git
cd PocketHunter-Suite

# Build and start all services
docker compose up --build
```

The application will be available at `http://localhost:8501`.

### Services

- **app**: Streamlit web interface (port 8501)
- **worker**: Celery worker for background task processing
- **redis**: Message broker for task queue

## Manual Installation

If you prefer to run without Docker:

### Prerequisites

- Python 3.8+
- Redis server
- PocketHunter CLI tools
- SMINA (for docking)

### Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Install docking dependencies (optional)
./install_docking_deps.sh

# Start Redis
redis-server

# Start Celery worker (in separate terminal)
celery -A celery_app worker --loglevel=info

# Run the application
streamlit run main.py
```

## Workflow

The pipeline consists of four sequential steps. Each step generates a unique Job ID that links to subsequent steps.

### Step 1: Extract Frames

Upload a trajectory file (XTC/TRR) and topology (PDB/GRO) to extract individual frames as PDB files.

**Parameters:**
- Frame interval (stride)
- Start/end frames

### Step 2: Detect Pockets

Run PocketHunter on extracted frames to identify binding pockets.

**Input:** Job ID from Step 1 or upload PDB files directly

**Output:** CSV file with pocket predictions including residues, coordinates, and probability scores

### Step 3: Cluster Pockets

Group similar pockets using DBSCAN clustering based on spatial overlap.

**Parameters:**
- Epsilon (cluster radius)
- Minimum samples per cluster

**Output:** Representative pockets from each cluster for docking

### Step 4: Molecular Docking

Dock ligands against representative pocket conformations using SMINA.

**Parameters:**
- Number of poses (1-50)
- Exhaustiveness (1-20)
- pH for protonation (4.0-10.0)
- Box dimensions (X, Y, Z in Angstroms)

**Input:**
- Job ID from clustering step
- Ligand files (PDBQT format, single files or ZIP archive)

**Output:**
- Docking scores and poses in SDF format
- Interactive results table with filtering
- 3D visualization of docked poses

## Architecture

```
                    +------------------+
                    |    Streamlit     |
                    |   (Frontend)     |
                    +--------+---------+
                             |
                    +--------v---------+
                    |      Redis       |
                    |  (Message Queue) |
                    +--------+---------+
                             |
                    +--------v---------+
                    |  Celery Worker   |
                    |   (Processing)   |
                    +------------------+
```

**Data Flow:**
1. User uploads files via Streamlit interface
2. Files stored in `uploads/` directory with job-specific paths
3. Celery worker processes tasks asynchronously
4. Results saved to `results/` directory
5. Status tracked via JSON files in `task_status/` directory

## File Structure

```
PocketHunter-Suite/
├── main.py                 # Application entry point
├── celery_app.py           # Celery configuration
├── tasks.py                # Background task definitions
├── session_state.py        # Session state management
├── extract_frames_app.py   # Step 1: Frame extraction
├── detect_pockets_app.py   # Step 2: Pocket detection
├── cluster_pockets_app.py  # Step 3: Pocket clustering
├── docking_app.py          # Step 4: Molecular docking
├── task_monitor_app.py     # Task monitoring dashboard
├── step4_docking.py        # Docking backend functions
├── uploads/                # User uploaded files
├── results/                # Processing results
└── task_status/            # Job status tracking
```

## Docking Box Configuration

The docking box defines the search space for ligand poses:

- **Center**: Automatically calculated from pocket residues
- **Size**: Configurable X, Y, Z dimensions (default: 20x20x20 Angstroms)

Larger boxes increase search space but require higher exhaustiveness for accurate results.

## Troubleshooting

**Tasks stuck in "running" state:**
- Check Celery worker logs for errors
- Verify Redis connection is active
- Restart the worker: `docker compose restart worker`

**No pockets detected:**
- Ensure PDB files contain protein atoms
- Check that PocketHunter is properly installed in the container

**Docking fails with "No PDBQT files found":**
- Verify ligand ZIP contains .pdbqt files (not nested in subdirectories)
- Check file format is valid PDBQT

**Browser shows stale data:**
- Refresh the page after task completion
- Check Task Monitor for actual job status

## Environment Variables

Configure via `.env` file or environment:

```
REDIS_URL=redis://localhost:6379/0
SMINA_PATH=/usr/local/bin/smina
POCKETHUNTER_PATH=/opt/pockethunter
```

## License

This project is open source. See LICENSE file for details.

## Contributing

Contributions welcome. Please open an issue or submit a pull request.
