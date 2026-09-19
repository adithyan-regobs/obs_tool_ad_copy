#!/bin/bash

# ObsTool Backend Start Script
# This script starts the FastAPI application with all necessary checks

set -e  # Exit on error

echo "============================================"
echo "  ObsTool Backend Startup Script"
echo "============================================"
echo ""

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to print colored output
print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    print_warning "Virtual environment not found. Creating..."
    python3 -m venv venv
    print_success "Virtual environment created"
fi

# Activate virtual environment
print_warning "Activating virtual environment..."
source venv/bin/activate
print_success "Virtual environment activated"

# Check if requirements.txt exists and install dependencies
if [ -f "requirements.txt" ]; then
    print_warning "Installing/updating dependencies..."
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    print_success "Dependencies installed"
else
    print_error "requirements.txt not found!"
    exit 1
fi

# Check if .env file exists
if [ ! -f ".env" ]; then
    print_error ".env file not found!"
    print_warning "Please create a .env file with the following variables:"
    echo "  DB_USER=postgres"
    echo "  DB_PASSWORD=your_password"
    echo "  DB_HOST=localhost"
    echo "  DB_PORT=5432"
    echo "  DB_NAME=app_db"
    echo "  DATADOG_API_KEY=your_api_key"
    echo "  DATADOG_APP_KEY=your_app_key"
    exit 1
fi

print_success ".env file found"

# Check if PostgreSQL is running
print_warning "Checking PostgreSQL connection..."
if docker ps | grep -q "pg"; then
    print_success "PostgreSQL container is running"
else
    print_error "PostgreSQL container 'pg' is not running!"
    print_warning "Starting PostgreSQL container..."
    docker start pg 2>/dev/null || {
        print_warning "Container doesn't exist. Creating new PostgreSQL container..."
        docker run -d --name pg \
            -e POSTGRES_PASSWORD=mysecretpassword \
            -p 5432:5432 \
            postgres:16
    }
    sleep 3
    print_success "PostgreSQL container started"
fi

# Test database connection
print_warning "Testing database connection..."
if docker exec pg psql -U postgres -d app_db -c "SELECT 1" > /dev/null 2>&1; then
    print_success "Database connection successful"
else
    print_error "Cannot connect to database 'app_db'"
    print_warning "Creating database..."
    docker exec pg psql -U postgres -c "CREATE DATABASE app_db" 2>/dev/null || true
    print_success "Database created"
fi

# Database is managed manually
print_success "Database is ready"

# Run database migrations
print_warning "Running database migrations..."
if alembic upgrade head; then
    print_success "Migrations completed successfully"
else
    print_error "Migration failed!"
    print_warning "Continuing to start application anyway..."
fi

# Get host IP for display
HOST=${HOST:-0.0.0.0}
PORT=${PORT:-8000}

echo ""
echo "============================================"
print_success "Starting ObsTool Backend Server"
echo "============================================"
echo ""
echo "  Server URL: http://localhost:${PORT}"
echo "  Docs URL:   http://localhost:${PORT}/docs"
echo "  ReDoc URL:  http://localhost:${PORT}/redoc"
echo ""
echo "  Press CTRL+C to stop the server"
echo ""
echo "============================================"
echo ""

# Start the server
# Using Gunicorn + Uvicorn workers for production
# --preload: Load app before forking workers (REQUIRED for macOS to avoid fork issues)
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
gunicorn app.main:app \
  --bind ${HOST}:${PORT} \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --preload \
  --timeout 300 \
  --access-logfile - \
  --error-logfile -
