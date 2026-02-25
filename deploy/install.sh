#!/bin/bash
# Deploy Matchbook trading system on a VPS
# Run: chmod +x install.sh && ./install.sh

set -e
cd "$(dirname "$0")/.."

echo "=== Matchbook Trading System - VPS Install ==="

# 1. Create venv if missing
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

# 2. Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# 3. Check .env exists
if [ ! -f ".env" ]; then
    echo "WARNING: .env not found. Copy from .env.example and add your credentials:"
    echo "  cp .env.example .env"
    echo "  nano .env"
    exit 1
fi

# 4. Install systemd services (requires sudo)
echo ""
echo "Installing systemd services..."
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
sudo tee /etc/systemd/system/matchbook-bot.service > /dev/null << EOF
[Unit]
Description=Matchbook Trading Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/venv/bin/python bot.py
Restart=always
RestartSec=10
Environment=PATH=$SCRIPT_DIR/venv/bin

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/matchbook-dashboard.service > /dev/null << EOF
[Unit]
Description=Matchbook Streamlit Dashboard
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/venv/bin/streamlit run app.py --server.address 0.0.0.0 --server.port 8501
Restart=always
RestartSec=10
Environment=PATH=$SCRIPT_DIR/venv/bin

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
echo ""
echo "=== Done ==="
echo ""
echo "Start services:"
echo "  sudo systemctl start matchbook-bot"
echo "  sudo systemctl start matchbook-dashboard"
echo ""
echo "Enable on boot:"
echo "  sudo systemctl enable matchbook-bot matchbook-dashboard"
echo ""
echo "View logs:"
echo "  journalctl -u matchbook-bot -f"
echo "  journalctl -u matchbook-dashboard -f"
echo ""
echo "Dashboard: http://YOUR_VPS_IP:8501"
echo ""
echo "Optional: Add firewall rule for port 8501:"
echo "  sudo ufw allow 8501/tcp && sudo ufw reload"
