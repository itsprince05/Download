# PocketFM Audio Capture Bot

Secure Python Telegram bot using Playwright to automate Chromium, capture audio streams from PocketFM, and send them to a Telegram group.

## Features

- **Browser Automation** — Playwright Chromium with full network interception
- **Audio Detection** — Scores and ranks audio URLs from all traffic (direct + HLS/M3U8)
- **Live Screenshot Streaming** — Full HD screenshots every 3s, edits same message
- **Group-Only Security** — Completely dead/silent in DMs and other groups
- **Auto-Retry Downloads** — 3 attempts with backoff
- **HLS Support** — Parses M3U8, picks highest quality variant
- **No Port Needed** — Uses Telegram long polling, safe alongside other VPS bots

## Files

```
pocketfm-bot/
├── bot.py                  # Entry point
├── config.py               # All configuration & credentials
├── logger.py               # Logging setup (file + console)
├── browser_manager.py      # Playwright Chromium controller
├── network_monitor.py      # Network traffic interceptor
├── screenshot_streamer.py  # Live screenshot sender
├── audio_downloader.py     # Audio file downloader (direct + HLS)
├── telegram_handler.py     # Telegram commands & capture pipeline
├── requirements.txt        # Python dependencies
├── deploy.sh               # One-command Ubuntu VPS deployment
└── README.md               # This file
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Show welcome message |
| `/capture` | Start full capture pipeline |
| `/status` | Check bot & browser status |
| `/screenshot` | Take a single screenshot |
| `/urls` | List all captured audio URLs |
| `/stop` | Stop capture & close browser |
| `/help` | Show help message |

## Deploy on Ubuntu VPS

### Quick (one command)

```bash
scp -r pocketfm-bot/ root@YOUR_VPS_IP:/opt/
ssh root@YOUR_VPS_IP
cd /opt/pocketfm-bot
chmod +x deploy.sh
sudo ./deploy.sh
sudo systemctl start pocketfm-bot
```

### Manual

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv ffmpeg \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 libx11-xcb1 \
    libxcomposite1 libxdamage1 libxrandr2 libatk1.0-0 libcups2 \
    libxss1 libgtk-3-0 fonts-liberation

cd /opt/pocketfm-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium
python bot.py
```

### Service Management

```bash
sudo systemctl start pocketfm-bot     # Start
sudo systemctl stop pocketfm-bot      # Stop
sudo systemctl restart pocketfm-bot   # Restart
sudo systemctl status pocketfm-bot    # Status
sudo journalctl -u pocketfm-bot -f    # Live logs
tail -f /opt/pocketfm-bot/bot.log     # App logs
```

## Port Safety

This bot uses **Telegram long polling** (`getUpdates`). It does **NOT** open any port.
Runs safely alongside your other 4-5 bots without conflicts.

The deploy script includes a `find_free_port()` function that scans 8100-8999 range
and picks the first unused port — reserved for future use if ever needed.
