#!/bin/bash

# VPS Deployment Script for Cache API
# This script automates deployment on a VPS.
# It is hardened for shared VPS usage by requiring project-specific
# service/directory naming to avoid collisions with other deployments.

set -e  # Exit on error

echo "=========================================="
echo "Cache API VPS Deployment Script"
echo "=========================================="
echo ""

# Configuration (override via env vars in CI or shell)
SERVICE_NAME="${SERVICE_NAME:-cache-api}"
SERVICE_DIR="${SERVICE_DIR:-/home/ubuntu/services/cache-api}"
VENV_DIR="$SERVICE_DIR/venv"
SERVICE_FILE="${SERVICE_FILE:-}"
REPO_URL="${REPO_URL:-https://github.com/joypciu/cache-api.git}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
API_PORT="${API_PORT:-5000}"
EXPECTED_REPO_SLUG="${EXPECTED_REPO_SLUG:-joypciu/cache-api}"
PREVIOUS_SERVICE_NAME="${PREVIOUS_SERVICE_NAME:-}"
SOURCE_REPO_SLUG="${SOURCE_REPO_SLUG:-unknown}"
PRIMARY_REPO_SLUG="${PRIMARY_REPO_SLUG:-joypciu/cache-api}"
ALLOW_PRIMARY_SERVICE_NAME="${ALLOW_PRIMARY_SERVICE_NAME:-false}"
PRODUCTION_SERVICE_NAME="${PRODUCTION_SERVICE_NAME:-cache-api}"
PRODUCTION_PORT="${PRODUCTION_PORT:-5000}"
NGINX_SITE_NAME="${NGINX_SITE_NAME:-${SERVICE_NAME}}"
PROTECTED_NGINX_SITE_NAME="${PROTECTED_NGINX_SITE_NAME:-cache-api}"
REQUIRE_UNIQUE_NAME="${REQUIRE_UNIQUE_NAME:-true}"
LOCK_FILE="/tmp/${SERVICE_NAME}.deploy.lock"

if [ -z "$PREVIOUS_SERVICE_NAME" ] && [[ "$SERVICE_NAME" == *-prod ]]; then
    PREVIOUS_SERVICE_NAME="${SERVICE_NAME%-prod}"
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored messages
print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_info() {
    echo -e "${YELLOW}→ $1${NC}"
}

# Lock to avoid parallel deploy races
if [ -f "$LOCK_FILE" ]; then
    print_error "Another deployment appears to be running for $SERVICE_NAME ($LOCK_FILE exists)"
    exit 1
fi
trap 'rm -f "$LOCK_FILE"' EXIT
touch "$LOCK_FILE"

# Check if running as correct user
if [ "$USER" != "ubuntu" ]; then
    print_error "This script should be run as the ubuntu user"
    exit 1
fi

print_info "Deploy target:"
echo "  SERVICE_NAME=$SERVICE_NAME"
echo "  SERVICE_DIR=$SERVICE_DIR"
echo "  DEPLOY_BRANCH=$DEPLOY_BRANCH"
echo "  API_PORT=$API_PORT"
echo "  REPO_URL=$REPO_URL"
echo "  PREVIOUS_SERVICE_NAME=${PREVIOUS_SERVICE_NAME:-<none>}"
echo "  SOURCE_REPO_SLUG=$SOURCE_REPO_SLUG"
echo "  PRIMARY_REPO_SLUG=$PRIMARY_REPO_SLUG"
echo "  NGINX_SITE_NAME=$NGINX_SITE_NAME"

if [ "$REQUIRE_UNIQUE_NAME" = "true" ] && [ "$SERVICE_NAME" = "cache-api" ]; then
    print_error "SERVICE_NAME=cache-api is not unique for shared VPS use."
    print_info "Set a unique SERVICE_NAME (example: cache-api-prod-joy) and matching SERVICE_DIR."
    exit 1
fi

if [ "$SERVICE_NAME" = "$PRODUCTION_SERVICE_NAME" ]; then
    if [ "$ALLOW_PRIMARY_SERVICE_NAME" != "true" ] || [ "$SOURCE_REPO_SLUG" != "$PRIMARY_REPO_SLUG" ]; then
        print_error "Protected service name ${PRODUCTION_SERVICE_NAME} is reserved for the primary repository only."
        print_info "source repo: $SOURCE_REPO_SLUG"
        print_info "primary repo: $PRIMARY_REPO_SLUG"
        print_info "For forks, use unique DEPLOY_SERVICE_NAME, DEPLOY_DIR, and DEPLOY_PORT."
        exit 1
    fi
fi

if [ "$ALLOW_PRIMARY_SERVICE_NAME" != "true" ] || [ "$SOURCE_REPO_SLUG" != "$PRIMARY_REPO_SLUG" ]; then
    if [ "$API_PORT" = "$PRODUCTION_PORT" ]; then
        print_error "Protected production port ${PRODUCTION_PORT} is reserved for the primary repository only."
        print_info "For forks, set a unique DEPLOY_PORT."
        exit 1
    fi

    if [ "$NGINX_SITE_NAME" = "$PROTECTED_NGINX_SITE_NAME" ]; then
        print_error "Protected nginx site ${PROTECTED_NGINX_SITE_NAME} is reserved for the primary repository only."
        print_info "For forks, set a unique DEPLOY_NGINX_SITE_NAME."
        exit 1
    fi

    if sudo test -e "/etc/nginx/sites-enabled/${NGINX_SITE_NAME}" || sudo test -e "/etc/nginx/sites-available/${NGINX_SITE_NAME}"; then
        print_error "Nginx site name ${NGINX_SITE_NAME} already exists on VPS."
        print_info "Use a unique DEPLOY_NGINX_SITE_NAME for fork deployments."
        exit 1
    fi
fi

# Guard against reusing an existing systemd unit unintentionally.
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
    print_info "Systemd unit ${SERVICE_NAME}.service already exists (will be updated)"
fi

# Check if Redis is installed
print_info "Checking Redis installation..."
if ! command -v redis-server &> /dev/null; then
    print_info "Redis not found. Installing Redis..."
    sudo apt update
    sudo apt install redis-server -y
    print_success "Redis installed"
else
    print_success "Redis is already installed"
fi

# Start and enable Redis
print_info "Configuring Redis..."
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Verify Redis is working
if redis-cli ping &> /dev/null; then
    print_success "Redis is running and responding"
else
    print_error "Redis is not responding"
    exit 1
fi

# Create service directory if it doesn't exist
if [ ! -d "$SERVICE_DIR" ]; then
    print_info "Creating service directory..."
    sudo mkdir -p "$SERVICE_DIR"
    sudo chown -R ubuntu:ubuntu "$(dirname "$SERVICE_DIR")"
    print_success "Service directory created"
fi

# Navigate to service directory
cd "$SERVICE_DIR"

# Check if this is first time setup or update
if [ ! -d ".git" ]; then
    print_info "First time setup - cloning repository..."
    git clone "$REPO_URL" .
    print_success "Repository cloned"
else
    print_info "Updating repository..."
    current_remote="$(git remote get-url origin 2>/dev/null || true)"
    if [ -n "$current_remote" ] && [[ "$current_remote" != *"$EXPECTED_REPO_SLUG"* ]]; then
        print_error "Repo mismatch in $SERVICE_DIR"
        echo "  expected remote containing: $EXPECTED_REPO_SLUG"
        echo "  actual remote: $current_remote"
        exit 1
    fi
    git fetch origin "$DEPLOY_BRANCH"
    git checkout -f "$DEPLOY_BRANCH"
    git reset --hard "origin/$DEPLOY_BRANCH"
    print_success "Repository updated"
fi

# Create virtual environment if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    print_info "Creating Python virtual environment..."
    python3 -m venv venv
    print_success "Virtual environment created"
fi

# Activate virtual environment and install dependencies
print_info "Installing/updating Python dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt --upgrade
print_success "Dependencies installed"

# Create .env file if it doesn't exist
if [ ! -f ".env" ]; then
    print_info "Creating .env file from template..."
    if [ -f ".env.example" ]; then
        cp .env.example .env
        print_success ".env file created"
        print_info "Please edit .env file with your configuration"
    else
        print_error ".env.example not found"
    fi
else
    print_success ".env file already exists"
fi

# Install systemd service
print_info "Installing systemd service..."
unit_installed_from_repo="false"
if [ -n "$SERVICE_FILE" ]; then
    if [ -f "$SERVICE_FILE" ]; then
        print_info "Using service file from repo: $SERVICE_FILE"
        if sudo cp "$SERVICE_FILE" "/etc/systemd/system/${SERVICE_NAME}.service"; then
            unit_installed_from_repo="true"
        else
            print_info "Could not copy $SERVICE_FILE; falling back to generated unit"
        fi
    else
        print_info "Requested service file not found: $SERVICE_FILE (will generate unit)"
    fi
fi

if [ "$unit_installed_from_repo" != "true" ]; then
    print_info "Service file not found in repo; creating generated unit ${SERVICE_NAME}.service"
    cat > "/tmp/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=${SERVICE_NAME}
After=network.target redis-server.service
Wants=redis-server.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=${SERVICE_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_DIR}/bin/python -m uvicorn main:app --host 0.0.0.0 --port ${API_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    sudo cp "/tmp/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
fi
sudo systemctl daemon-reload
print_success "Systemd service installed"

# Enable and restart service
print_info "Starting $SERVICE_NAME service..."
sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true

# If another process occupies API_PORT, attempt safe takeover from legacy service.
port_line="$(sudo ss -ltnp "sport = :${API_PORT}" 2>/dev/null | awk 'NR>1 && /LISTEN/{print; exit}')"
if [ -n "$port_line" ]; then
    port_pid="$(echo "$port_line" | sed -n 's/.*pid=\([0-9]\+\).*/\1/p')"
    owner_unit=""

    if [ -n "$port_pid" ] && [ -r "/proc/${port_pid}/cgroup" ]; then
        owner_unit="$(grep -oE '[^/]+\.service' "/proc/${port_pid}/cgroup" | head -n 1 || true)"
    fi

    print_info "Port ${API_PORT} currently in use: ${port_line}"

    if [ "$owner_unit" = "${SERVICE_NAME}.service" ]; then
        print_info "Port is still owned by ${SERVICE_NAME}.service; waiting for release..."
        sleep 2
    elif [ -n "$PREVIOUS_SERVICE_NAME" ] && [ "$owner_unit" = "${PREVIOUS_SERVICE_NAME}.service" ]; then
        print_info "Port is owned by legacy unit ${PREVIOUS_SERVICE_NAME}.service; stopping it for takeover..."
        sudo systemctl stop "$PREVIOUS_SERVICE_NAME"
        sleep 2
    else
        print_error "Port ${API_PORT} is occupied by an unrelated process/unit; refusing to kill it."
        if [ -n "$owner_unit" ]; then
            print_info "Detected owner unit: ${owner_unit}"
        fi
        print_info "Use a different DEPLOY_PORT or set PREVIOUS_SERVICE_NAME to allow controlled takeover."
        exit 1
    fi
fi

# Final hard check before start.
if sudo ss -ltnp | grep -q ":${API_PORT} "; then
    print_error "Port ${API_PORT} is still in use after takeover attempt."
    print_info "Port owner details:"
    sudo ss -ltnp | grep ":${API_PORT} " || true
    exit 1
fi

sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

# Wait for service to start
sleep 3

# Check service status
if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    print_success "$SERVICE_NAME service is running"
else
    print_error "$SERVICE_NAME service failed to start"
    print_info "Checking logs..."
    sudo journalctl -u "$SERVICE_NAME" -n 20 --no-pager
    exit 1
fi

# Verify API is responding
print_info "Verifying API is responding..."
if curl -fsS "http://localhost:${API_PORT}/" &> /dev/null; then
    print_success "API is responding on port ${API_PORT}"
else
    print_error "API is not responding"
fi

# Show status
echo ""
echo "=========================================="
echo "Deployment Summary"
echo "=========================================="
echo ""

# Service status
print_info "Service Status:"
sudo systemctl status "$SERVICE_NAME" --no-pager | head -n 10

echo ""

# Redis status
print_info "Redis Status:"
sudo systemctl status redis-server --no-pager | head -n 5

echo ""

# Port status
print_info "Port ${API_PORT} Status:"
sudo netstat -tlnp | grep ":${API_PORT}" || echo "Port not listening"

echo ""

# Cache statistics
print_info "Cache Statistics:"
if [ -n "${ADMIN_TOKEN:-}" ]; then
    curl -s -H "Authorization: Bearer ${ADMIN_TOKEN}" "http://localhost:${API_PORT}/cache/stats" | python3 -m json.tool 2>/dev/null || echo "Could not fetch cache stats"
else
    echo "ADMIN_TOKEN not set; skipping /cache/stats check"
fi

echo ""
echo "=========================================="
print_success "Deployment completed successfully!"
echo "=========================================="
echo ""
print_info "Useful commands:"
echo "  View logs:         sudo journalctl -u $SERVICE_NAME -f"
echo "  Restart service:   sudo systemctl restart $SERVICE_NAME"
echo "  Check status:      sudo systemctl status $SERVICE_NAME"
echo "  Clear cache:       curl -X DELETE http://localhost:${API_PORT}/cache/clear"
echo ""
