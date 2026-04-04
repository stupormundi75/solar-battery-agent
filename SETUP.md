# Solar Battery Agent — Setup Guide

## Architecture (simple!)

```
cron (every hour)
  → agent.py
      → fetch SE4 prices (elprisetjustnu.se)
      → fetch solar forecast (open-meteo.com)
      → decide: Charge or Stop
      → call iSolarCloud API
      → write public/status.json
      → send email notification

browser → battery.godaly.com
  → Apache serves public/index.html (static)
  → index.html fetches status.json
  → shows dashboard
```

No Flask. No Passenger. No web framework. Just Python + cron + static files.

---

## File structure on server

```
~/solar-agent/
├── agent.py          ← the agent (run by cron)
├── config.json       ← your settings (not in git)
├── token.json        ← OAuth2 token (not in git)
├── logs/
│   └── agent.log     ← agent activity log
└── public/
    ├── index.html    ← static dashboard (served by Apache)
    └── status.json   ← written by agent after each run
```

```
~/battery/            ← battery.godaly.com web root
    → symlink or copy of public/ contents
```

---

## Installation

### 1. Clone repo on server

```bash
ssh godaly.com@linux290.unoeuro.com
cd ~/solar-agent
git pull
```

### 2. Install requests for Python 3.11

```bash
/opt/alt/python311/bin/pip3.11 install --user requests
```

Or check if it's available:
```bash
/opt/alt/python311/bin/python3.11 -c "import requests; print('OK')"
```

### 3. Create config.json

```bash
cp ~/solar-agent/config.example.json ~/solar-agent/config.json
nano ~/solar-agent/config.json
```

### 4. Link public folder to battery web root

```bash
cp ~/solar-agent/public/index.html ~/battery/index.html
# Agent will write status.json directly to ~/battery/
```

Update STATUS_FILE path in agent.py:
```bash
# Edit agent.py line: STATUS_FILE = BASE_DIR / "public" / "status.json"
# Change to:          STATUS_FILE = Path("/var/www/godaly.com/battery/status.json")
```

### 5. Set up cron

```bash
crontab -e
```

Add this line:
```
2 * * * * /opt/alt/python311/bin/python3.11 /var/www/godaly.com/solar-agent/agent.py >> /var/www/godaly.com/solar-agent/logs/cron.log 2>&1
```

### 6. Test manually

```bash
/opt/alt/python311/bin/python3.11 ~/solar-agent/agent.py
cat ~/battery/status.json
```

Visit http://battery.godaly.com — you should see the dashboard.

---

## Email notifications

The agent sends an email after every run. To configure:

1. Find your simply.com SMTP settings in their control panel
2. Add them to config.json under the `smtp` section
3. Set `notify_email` to where you want notifications sent

---

## Re-authorization (when token expires)

Tokens last ~48 hours. When expired:

1. On your laptop: `python3 test_isolarcloud.py authorize`
2. Visit the URL, approve, copy the code
3. Run: `python3 test_isolarcloud.py pysolar CODE`
4. Upload new token: `scp token.json godaly.com@linux290.unoeuro.com:~/solar-agent/`

---

## Dry run mode

Set `"dry_run": true` in config.json to run without touching the battery.
Agent will log and email what it *would* do. Good for testing.
