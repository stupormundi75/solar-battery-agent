# Solar Battery Agent ⚡☀️

A weather-aware battery charging agent for Sungrow/iSolarCloud systems in Sweden.

Runs as a Flask web app on simply.com shared hosting via Phusion Passenger WSGI.
Triggered hourly by simply.com's URL cron system.

## What it does

Every hour the agent:

1. Fetches **SE4 spot prices** from [elprisetjustnu.se](https://www.elprisetjustnu.se/elpris-api) (Nordpool data, free)
2. Fetches **solar radiation forecast** from [Open-Meteo](https://open-meteo.com) (free, no key needed)
3. Applies this logic:

```
Is GHI forecast ≥ threshold this hour?
  YES → Solar mode: let panels charge the battery, no grid charging
  NO  → Is this one of the N cheapest hours today?
          YES → Force charge from grid
          NO  → Normal self-consumption mode
```

4. Calls the **iSolarCloud API** to set the battery mode
5. Persists the result to `state.json` for the web dashboard

## Web dashboard

A dark, real-time dashboard shows:
- Current mode (Solar / Grid Charge / Normal)
- Today's spot prices with cheap hours highlighted
- Solar radiation forecast vs threshold
- 24h schedule timeline
- Activity log
- Configuration editor

## Data sources

| Source | Data | Cost |
|---|---|---|
| [elprisetjustnu.se](https://www.elprisetjustnu.se/elpris-api) | SE1–SE4 Nordpool spot prices | Free, no key |
| [Open-Meteo](https://open-meteo.com) | Solar radiation (GHI W/m²) | Free, no key |
| [iSolarCloud](https://developer-api.isolarcloud.com) | Battery control API | Free, requires approval |

## Setup

### 1. Clone on the server

```bash
ssh yourusername@yourdomain.com
git clone https://github.com/YOURUSERNAME/solar-battery-agent.git ~/solar-agent
```

### 2. Install dependencies

```bash
python3 -m pip install --user flask==2.0.3 requests backports.zoneinfo
```

### 3. Create config.json

```bash
cp ~/solar-agent/config.example.json ~/solar-agent/config.json
nano ~/solar-agent/config.json
```

Fill in your:
- GPS coordinates (latitude/longitude)
- iSolarCloud username, password, app_key, app_secret, plant_id
- `cron_secret` — any random string, used to protect the `/cron/run` endpoint

### 4. Get iSolarCloud API credentials

1. Register at https://developer-api.isolarcloud.com
2. Applications → Create (select **without OAuth2**)
3. Wait 1–2 days for approval
4. Action → View → copy `app_key` and `app_secret`
5. Find your `plant_id` from the URL on https://web3.isolarcloud.eu

### 5. Set up Passenger on simply.com

Create a subdomain (e.g. `battery.yourdomain.com`) in the simply.com control panel.
Then via SSH:

```bash
# Create the public folder Passenger watches
mkdir -p /var/www/battery.yourdomain.com/public
mkdir -p /var/www/battery.yourdomain.com/tmp

# Copy the Passenger entry point
cp ~/solar-agent/passenger_wsgi.py /var/www/battery.yourdomain.com/

# Create .htaccess
cat > /var/www/battery.yourdomain.com/public/.htaccess << 'EOF'
PassengerEnabled on
PassengerAppRoot /var/www/battery.yourdomain.com
PassengerPython /usr/bin/python3
Options -Indexes
EOF

# Touch restart file
touch /var/www/battery.yourdomain.com/tmp/restart.txt
```

### 6. Set up hourly cron

In simply.com control panel → Website → Cron jobs, add a URL cron:

```
URL:      https://battery.yourdomain.com/cron/run?secret=YOURCRONKEY
Interval: Every 1 hour
```

The secret must match `cron_secret` in your `config.json`.

---

## Deploying updates

```bash
# Local machine
git add .
git commit -m "your change description"
git push

# Server
cd ~/solar-agent
git pull
touch /var/www/battery.yourdomain.com/tmp/restart.txt
```

## Configuration

| Setting | Default | Description |
|---|---|---|
| `latitude` / `longitude` | 55.6, 13.0 | Your location (Malmö area default) |
| `price_zone` | `SE4` | SE1 / SE2 / SE3 / SE4 |
| `cheap_hours_per_day` | `3` | How many cheapest hours to grid-charge per day |
| `solar_ghi_threshold` | `100` | W/m² above which solar is considered generating |
| `battery_charge_power` | `3000` | Watts to charge at during grid-charge hours |
| `cron_secret` | `""` | Random string to protect `/cron/run` endpoint |

All settings are also editable via the web dashboard's Configuration panel.

## Notes

- **Dry run mode**: if iSolarCloud credentials are not configured, the agent
  computes and logs the schedule but does not call the API. Good for testing.
- **State persistence**: `state.json` is written after every run so the dashboard
  always shows current data even after Passenger restarts the process.
- **Quarter-hour prices**: since October 2025 Swedish electricity is priced per
  15 minutes (96 values/day). The agent averages these to hourly automatically.
