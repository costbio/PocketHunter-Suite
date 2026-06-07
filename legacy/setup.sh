#!/bin/bash

# PocketHunter Streamlit App Setup Script
# This script helps set up the Streamlit PocketHunter application

echo "🧬 Setting up PocketHunter Streamlit App..."
echo "=========================================="

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.8+ first."
    exit 1
fi

# Check Python version
python_version=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "✅ Python version: $python_version"

# Check if pip is installed
if ! command -v pip3 &> /dev/null; then
    echo "❌ pip3 is not installed. Please install pip first."
    exit 1
fi

# Create virtual environment
echo "📦 Creating virtual environment..."
python3 -m venv venv

# Activate virtual environment
echo "🔧 Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "⬆️ Upgrading pip..."
pip install --upgrade pip

# Install requirements
echo "📚 Installing dependencies..."
pip install -r requirements.txt

# Check if Redis is installed
if ! command -v redis-server &> /dev/null; then
    echo "⚠️  Redis is not installed. Please install Redis:"
    echo "   Ubuntu/Debian: sudo apt-get install redis-server"
    echo "   macOS: brew install redis"
    echo "   Or download from: https://redis.io/download"
    echo ""
    echo "After installing Redis, run: redis-server"
else
    echo "✅ Redis is installed"
fi

# Create necessary directories
echo "📁 Creating directories..."
mkdir -p uploads
mkdir -p results
mkdir -p PocketHunter

# Copy PocketHunter from existing installation if available
if [ -d "../shiny_pockethunter_webapp/PocketHunter" ]; then
    echo "📋 Copying PocketHunter from existing installation..."
    cp -r ../shiny_pockethunter_webapp/PocketHunter/* PocketHunter/
elif [ -d "../Streamlit_Dockspot/PocketHunter" ]; then
    echo "📋 Copying PocketHunter from Streamlit_Dockspot..."
    cp -r ../Streamlit_Dockspot/PocketHunter/* PocketHunter/
else
    echo "⚠️  PocketHunter directory not found. Please ensure PocketHunter is installed in the PocketHunter/ directory."
fi

# Create .env file
echo "⚙️ Creating .env file..."
cat > .env << EOF
# Redis configuration
REDIS_URL=redis://localhost:6379/0

# Celery configuration
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

# File paths
UPLOAD_DIR=./uploads
RESULTS_DIR=./results
EOF

echo ""
echo "🎉 Setup complete!"
echo ""
echo "📋 Next steps:"
echo "1. Start Redis server: redis-server"
echo "2. Start Celery worker: celery -A celery_app worker --loglevel=info"
echo "3. Run the Streamlit app: streamlit run main.py"
echo ""
echo "🌐 The app will be available at: http://localhost:8501"
echo ""
echo "📖 For more information, see README.md" 