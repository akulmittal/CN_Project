#!/usr/bin/env bash
# nginx setup for CN Project demo
# Run with: sudo ./setup_nginx.sh
# Or run commands manually if sudo prompt doesn't work

set -euo pipefail

PROJECT_ROOT="/home/shankar-nag/CN_Project"
EXAM_DIR="$PROJECT_ROOT/online_exam_mvp"
NGINX_CONF="$PROJECT_ROOT/nginx-exam.conf"

echo "=== Setting up nginx for CN Project demo ==="

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo "This script needs root privileges."
    echo "Run with: sudo $0"
    echo ""
    echo "Or run these commands manually:"
    echo "  sudo cp $NGINX_CONF /etc/nginx/sites-available/exam"
    echo "  sudo ln -sf /etc/nginx/sites-available/exam /etc/nginx/sites-enabled/"
    echo "  sudo cp $EXAM_DIR/web/index.html /var/www/html/index.html"
    echo "  sudo nginx -t && sudo systemctl reload nginx"
    exit 1
fi

# Install nginx config
echo "Copying nginx config..."
cp "$NGINX_CONF" /etc/nginx/sites-available/exam

# Enable site
echo "Enabling site..."
ln -sf /etc/nginx/sites-available/exam /etc/nginx/sites-enabled/

# Copy frontend
echo "Copying frontend..."
cp "$EXAM_DIR/web/index.html" /var/www/html/index.html

# Test and start/reload nginx
echo "Testing nginx config..."
if nginx -t; then
    if systemctl is-active --quiet nginx; then
        echo "Reloading nginx..."
        systemctl reload nginx
    else
        echo "Starting nginx..."
        systemctl start nginx
    fi
    echo ""
    echo "=== Nginx setup complete ==="
    echo "Frontend:  http://localhost"
    echo "API:       http://localhost/backend/api/v1/"
    echo "Health:    http://localhost/backend/health"
else
    echo "ERROR: nginx config test failed"
    exit 1
fi