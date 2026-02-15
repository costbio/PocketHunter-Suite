FROM python:3.10-slim

# Install system dependencies, build tools, and Java (for P2Rank)
RUN apt-get update && apt-get install -y \
    curl \
    gcc \
    g++ \
    make \
    python3-dev \
    libxrender1 \
    libxext6 \
    default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc g++ make python3-dev \
    && apt-get autoremove -y

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p uploads results logs

# Expose Streamlit port
EXPOSE 8501

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# Default command (can be overridden in docker-compose)
CMD ["streamlit", "run", "main.py", "--server.port=8501", "--server.address=0.0.0.0"]
