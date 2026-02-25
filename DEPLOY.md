# Deploy on VPS

## Option A: systemd (recommended)

1. **Upload project** to your VPS (e.g. `scp -r aladin3 user@vps-ip:~`)

2. **Create `.env`** with your Matchbook credentials:
   ```bash
   cp .env.example .env
   nano .env
   ```

3. **Run install script:**
   ```bash
   cd aladin3
   chmod +x deploy/install.sh
   ./deploy/install.sh
   ```

4. **Start services:**
   ```bash
   sudo systemctl start matchbook-bot matchbook-dashboard
   sudo systemctl enable matchbook-bot matchbook-dashboard  # auto-start on reboot
   ```

5. **Open dashboard:** `http://YOUR_VPS_IP:8501`

6. **Firewall** (if needed):
   ```bash
   sudo ufw allow 8501/tcp
   sudo ufw reload
   ```

---

## Option B: Docker

1. **Upload project** and ensure `.env` exists

2. **Run:**
   ```bash
   docker compose up -d
   ```

3. **Dashboard:** `http://YOUR_VPS_IP:8501`

---

## Option C: Manual (quick test)

```bash
cd aladin3
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Terminal 1 - Bot
nohup python bot.py > bot.log 2>&1 &

# Terminal 2 - Dashboard (bind to all interfaces)
nohup streamlit run app.py --server.address 0.0.0.0 > dashboard.log 2>&1 &
```

---

## Useful commands

| Action | Command |
|--------|---------|
| Bot logs | `journalctl -u matchbook-bot -f` |
| Dashboard logs | `journalctl -u matchbook-dashboard -f` |
| Restart bot | `sudo systemctl restart matchbook-bot` |
| Stop all | `sudo systemctl stop matchbook-bot matchbook-dashboard` |
