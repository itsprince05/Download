#!/bin/bash
# ================================================================
# PocketFM Audio Capture Bot — Ubuntu VPS Deployment Script
# ================================================================
# Usage: chmod +x deploy.sh && sudo ./deploy.sh
# ================================================================
# This bot uses Telegram long polling — NO port needed.
# Safe alongside your other 4-5 bots.
# ================================================================

set -e

echo "================================================"
echo " PocketFM Bot — VPS Deployment"
echo "================================================"

# ── Show currently used ports ───────────────────────────────────
echo "[INFO] Currently used ports on this VPS:"
ss -tlnp 2>/dev/null | grep LISTEN | awk '{print $4}' | sort -u || echo "  (none or ss not available)"
echo ""

# ── Function: Find a free port (stored for future use) ──────────
find_free_port() {
    local port
    for port in $(seq 8100 8999); do
        if ! ss -tlnp 2>/dev/null | grep -q ":${port} "; then
            echo "$port"
            return 0
        fi
    done
    python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()"
}

FREE_PORT=$(find_free_port)
echo "[INFO] Next free port: $FREE_PORT (not used — bot runs on polling)"
echo ""

# ── System update ───────────────────────────────────────────────
echo "[1/7] Updating system packages..."
apt update && apt upgrade -y

# ── Install dependencies ────────────────────────────────────────
echo "[2/7] Installing Python, ffmpeg, and browser dependencies..."
apt install -y \
    python3 python3-pip python3-venv \
    wget curl unzip git \
    ffmpeg \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 libx11-xcb1 \
    libxcomposite1 libxdamage1 libxrandr2 libatk1.0-0 libcups2 \
    libxss1 libgtk-3-0 fonts-liberation xdg-utils

# ── Project directory ───────────────────────────────────────────
PROJECT_DIR="/opt/pocketfm-bot"
echo "[3/7] Setting up $PROJECT_DIR..."

if [ -d "$PROJECT_DIR" ]; then
    echo "  Backing up existing installation..."
    cp -r "$PROJECT_DIR" "${PROJECT_DIR}.bak.$(date +%s)"
fi

mkdir -p "$PROJECT_DIR"

# Copy all flat .py files and other needed files
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/main.py" ]; then
    echo "  Copying project files..."
    cp -f "$SCRIPT_DIR"/*.py "$PROJECT_DIR/"
    cp -f "$SCRIPT_DIR"/requirements.txt "$PROJECT_DIR/"
    cp -f "$SCRIPT_DIR"/deploy.sh "$PROJECT_DIR/" 2>/dev/null || true
    cp -f "$SCRIPT_DIR"/README.md "$PROJECT_DIR/" 2>/dev/null || true
fi

cd "$PROJECT_DIR"

# ── Python venv ─────────────────────────────────────────────────
echo "[4/7] Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# ── Install Python packages ────────────────────────────────────
echo "[5/7] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# ── Install Playwright Chromium ─────────────────────────────────
echo "[6/7] Installing Playwright Chromium..."
playwright install chromium
playwright install-deps chromium

# ── Directories ─────────────────────────────────────────────────
mkdir -p downloads screenshots

# ── systemd service ─────────────────────────────────────────────
echo "[7/7] Setting up systemd service..."

PYTHON_PATH="$PROJECT_DIR/venv/bin/python"

cat > /etc/systemd/system/pocketfm-bot.service << SERVICEEOF
[Unit]
Description=PocketFM Audio Capture Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON_PATH main.py
Restart=always
RestartSec=10
StartLimitIntervalSec=60
StartLimitBurst=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:$PROJECT_DIR/bot_stdout.log
StandardError=append:$PROJECT_DIR/bot_stderr.log
LimitNOFILE=65536
MemoryMax=1G
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable pocketfm-bot.service

echo ""
echo "================================================"
echo " DEPLOYMENT COMPLETE!"
echo "================================================"
echo ""
echo " This bot uses POLLING — no port conflicts."
echo ""
echo " Commands:"
echo "   sudo systemctl start pocketfm-bot"
echo "   sudo systemctl stop pocketfm-bot"
echo "   sudo systemctl restart pocketfm-bot"
echo "   sudo systemctl status pocketfm-bot"
echo "   sudo journalctl -u pocketfm-bot -f"
echo "   tail -f $PROJECT_DIR/bot.log"
echo ""
echo "================================================"
