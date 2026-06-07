#!/bin/bash

# PocketHunter Streamlit App Startup Script
# This script properly activates the conda environment and starts the app

echo "🚀 Starting PocketHunter Streamlit App..."

# Check if conda is available
if ! command -v conda &> /dev/null; then
    echo "❌ Error: conda is not installed or not in PATH"
    echo "Please install Anaconda or Miniconda first"
    exit 1
fi

# Activate the conda environment
echo "📦 Activating conda environment 'dockspot'..."
source $(conda info --base)/etc/profile.d/conda.sh
conda activate dockspot

if [ $? -ne 0 ]; then
    echo "❌ Error: Failed to activate conda environment 'dockspot'"
    echo "Please make sure the environment exists: conda create -n dockspot python=3.8"
    exit 1
fi

echo "✅ Conda environment activated successfully"

# Check if we're in the right directory
if [ ! -f "main.py" ]; then
    echo "❌ Error: main.py not found in current directory"
    echo "Please run this script from the streamlit_pockethunter_app directory"
    exit 1
fi

# Check if Redis is running
echo "🔍 Checking Redis server..."
if ! pgrep -x "redis-server" > /dev/null; then
    echo "⚠️  Warning: Redis server is not running"
    echo "Starting Redis server..."
    redis-server --daemonize yes
    sleep 2
fi

# Check if Celery worker is running
echo "🔍 Checking Celery worker..."
if ! pgrep -f "celery.*worker" > /dev/null; then
    echo "⚠️  Warning: Celery worker is not running"
    echo "Starting Celery worker in background..."
    celery -A celery_app worker --loglevel=info --detach
    sleep 3
fi

echo "✅ All services are running"

# Start the Streamlit app
echo "🌐 Starting Streamlit app..."
echo "📱 The app will be available at: http://localhost:8501"
echo "🔄 Press Ctrl+C to stop the app"
echo ""

# Start Streamlit with proper configuration
streamlit run main.py \
    --server.port 8501 \
    --server.address localhost \
    --server.headless false \
    --browser.gatherUsageStats false \
    --logger.level info 