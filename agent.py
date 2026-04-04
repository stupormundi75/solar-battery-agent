#!/opt/alt/python311/bin/python3.11
"""
Solar Battery Agent
===================
Runs every hour via cron. Fetches spot prices + solar forecast,
decides battery mode, sets it via iSolarCloud API, writes status.json.

Cron entry (run as your user):
  2 * * * * /opt/alt/python311/bin/python3.11 /var/www/godaly.com/solar-agent/agent.py

No web server needed. Dashboard reads status.json via static Apache.
"""

import json
import logging
import smtplib
import sys
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

# Use requests from --user installed packages
sys.path.insert(0, "/var/www/godaly.com/.local/lib/python3.6/site-packages")

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
TOKEN_FILE  = BASE_DIR / "token.json"
STATUS_FILE = Path("/var/www/godaly.com/battery/status.json"
LOG_FILE    = BASE_DIR / "logs" / "agent.log"
LOG_FILE.parent.mkdir(exist_ok=True)
STATUS_FILE.parent.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE)),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "latitude":            55.702,
    "longitude":           13.163,
    "price_zone":          "SE4",
    "cheap_hours_per_day": 3,
    "solar_ghi_threshold": 100,
    "dry_run":             False,
    "notify_email":        "",
    "smtp": {
        "host":     "mail.godaly.com",
        "port":     587,
        "username": "",
        "password": "",
        "from":     ""
    },
    "isolarcloud": {
        "app_key":      "",
        "app_secret":   "",
        "app_id":       "3251",
        "plant_id":     "5486815",
        "device_uuid":  "4033562",
        "redirect_uri": "https://battery.godaly.com/auth/callback",
    }
}

ISOLARCLOUD_BASE = "https://gateway.isolarcloud.eu"


def load_config():
    if CONFIG_FILE.exists():
        with open(str(CONFIG_FILE)) as f:
            saved = json.load(f)
        merged = {}
        merged.update(DEFAULT_CONFIG)
        merged.update(saved)
        iso = {}
        iso.update(DEFAULT_CONFIG["isolarcloud"])
        iso.update(saved.get("isolarcloud", {}))
        merged["isolarcloud"] = iso
        smtp = {}
        smtp.update(DEFAULT_CONFIG["smtp"])
        smtp.update(saved.get("smtp", {}))
        merged["smtp"] = smtp
        return merged
    return dict(DEFAULT_CONFIG)

# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def load_token():
    if TOKEN_FILE.exists():
        with open(str(TOKEN_FILE)) as f:
            return json.load(f)
    return None


def save_token(token):
    with open(str(TOKEN_FILE), "w") as f:
        json.dump(token, f, indent=2)

# ---------------------------------------------------------------------------
# iSolarCloud
# ---------------------------------------------------------------------------

def isolar_headers(app_secret, access_token=None):
    h = {"Content-Type": "application/json", "x-access-key": app_secret}
    if access_token:
        h["Authorization"] = "Bearer {}".format(access_token)
    return h


def isolar_body(data, app_key):
    body = dict(data)
    body["appkey"] = app_key
    body["lang"]   = "_en_US"
    return body


def isolar_post(path, data, iso_cfg, access_token=None):
    resp = requests.post(
        "{}{}".format(ISOLARCLOUD_BASE, path),
        headers=isolar_headers(iso_cfg["app_secret"], access_token),
        json=isolar_body(data, iso_cfg["app_key"]),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_valid_token(iso_cfg):
    token = load_token()
    if not token:
        log.error("No token.json — run authorization first")
        return None

    expires_at = token.get("expires_at", 0)
    now        = int(datetime.now(timezone.utc).timestamp())

    if expires_at - now < 60:
        log.info("Token expiring — refreshing...")
        try:
            data = isolar_post(
                "/openapi/apiManage/refreshToken",
                {"refresh_token": token["refresh_token"]},
                iso_cfg,
            )
            if "access_token" in data:
                token.update(data)
                token["expires_at"] = now + int(data.get("expires_in", 172799)) - 20
                save_token(token)
                log.info("Token refreshed")
            else:
                log.error("Token refresh failed: {}".format(data))
                return None
        except Exception as e:
            log.error("Token refresh error: {}".format(e))
            return None

    return token.get("access_token")


def set_battery_command(iso_cfg, command, dry_run=False):
    """
    command: 'Charge' | 'Stop'
    param_code 10004 = charge_discharge_command
    Charge=170, Stop=204
    """
    if dry_run:
        log.info("DRY RUN — would set battery to: {}".format(command))
        return {"status": "dry_run", "command": command}

    if not iso_cfg.get("app_key") or not iso_cfg.get("app_secret"):
        log.error("iSolarCloud credentials not configured")
        return {"status": "error", "msg": "not configured"}

    access_token = get_valid_token(iso_cfg)
    if not access_token:
        return {"status": "error", "msg": "no valid token"}

    value_map = {"Charge": "170", "Stop": "204", "Discharge": "187"}
    set_value  = value_map.get(command, "204")

    try:
        result = isolar_post(
            "/openapi/platform/paramSetting",
            {
                "set_type":      0,
                "uuid":          str(iso_cfg["device_uuid"]),
                "task_name":     "BatteryAgent {}".format(
                    datetime.now().strftime("%Y-%m-%d %H:%M")),
                "expire_second": 120,
                "param_list":    [{"param_code": "10004", "set_value": set_value}],
            },
            iso_cfg,
            access_token,
        )

        success = (
            result.get("result_code") == "1"
            and result.get("result_data", {}).get("check_result") == "1"
            and result.get("result_data", {}).get(
                "dev_result_list", [{}])[0].get("code") == "1"
        )

        if success:
            log.info("Battery set to {} OK".format(command))
            return {"status": "ok", "command": command}
        else:
            log.error("Battery command failed: {}".format(result))
            return {"status": "error", "msg": str(result)}

    except Exception as e:
        log.error("set_battery_command failed: {}".format(e))
        return {"status": "error", "msg": str(e)}

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_spot_prices(zone, date):
    url = (
        "https://www.elprisetjustnu.se/api/v1/prices/"
        "{}/{:02d}-{:02d}_{}.json".format(
            date.year, date.month, date.day, zone)
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        hour_prices = defaultdict(list)
        for item in data:
            h = datetime.fromisoformat(item["time_start"]).hour
            hour_prices[h].append(item["SEK_per_kWh"])

        return [
            {"hour": h, "SEK_per_kWh": round(sum(v)/len(v), 4)}
            for h, v in sorted(hour_prices.items())
        ]
    except Exception as e:
        log.error("Failed to fetch spot prices: {}".format(e))
        return []


def fetch_solar_forecast(lat, lon, date):
    """Fetch 48h solar forecast (today + tomorrow)."""
    from datetime import timedelta
    date_str      = date.strftime("%Y-%m-%d")
    tomorrow_str  = (date + timedelta(days=1)).strftime("%Y-%m-%d")
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude={}&longitude={}"
        "&hourly=shortwave_radiation"
        "&start_date={}&end_date={}"
        "&timezone=Europe/Stockholm"
    ).format(lat, lon, date_str, tomorrow_str)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        times = data["hourly"]["time"]
        ghi   = data["hourly"]["shortwave_radiation"]
        return [
            {
                "datetime": t,
                "date":     t[:10],
                "hour":     datetime.fromisoformat(t).hour,
                "ghi":      round(g or 0, 1),
            }
            for t, g in zip(times, ghi)
        ]
    except Exception as e:
        log.error("Failed to fetch solar forecast: {}".format(e))
        return []

# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def compute_schedule_for_day(cfg, prices, solar_slots):
    """Compute 24-slot schedule for a single day."""
    threshold = cfg["solar_ghi_threshold"]
    n_cheap   = cfg["cheap_hours_per_day"]

    ghi_by_hour   = {s["hour"]: s["ghi"] for s in solar_slots}
    price_by_hour = {p["hour"]: p["SEK_per_kWh"] for p in prices}

    solar_hours = {h for h, g in ghi_by_hour.items() if g >= threshold}
    non_solar_prices = sorted(
        [(h, price_by_hour[h]) for h in price_by_hour if h not in solar_hours],
        key=lambda x: x[1]
    )
    cheap_hours = {h for h, _ in non_solar_prices[:n_cheap]}
    cheap_rank  = {h: i+1 for i, (h, _) in enumerate(non_solar_prices)}

    slots = []
    for hour in range(24):
        price = price_by_hour.get(hour)
        ghi   = ghi_by_hour.get(hour, 0)

        if hour in solar_hours:
            mode   = "solar"
            reason = "Solar generating ({:.0f} W/m2 >= {})".format(ghi, threshold)
        elif hour in cheap_hours:
            mode   = "grid_charge"
            reason = "Cheapest non-solar hour (rank #{})".format(
                cheap_rank.get(hour, "?"))
        else:
            mode   = "normal"
            reason = "Self-consumption"

        slots.append({
            "hour":      hour,
            "price_SEK": round(price, 4) if price is not None else None,
            "ghi_W_m2":  ghi,
            "mode":      mode,
            "reason":    reason,
        })
    return slots


def compute_schedule(cfg, prices, solar):
    """Compute today schedule (used for battery control)."""
    today = solar[0]["date"] if solar and "date" in solar[0] else None
    today_solar = [s for s in solar if s.get("date") == today] if today else solar
    return compute_schedule_for_day(cfg, prices, today_solar)

# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def send_email(cfg, subject, body):
    smtp = cfg.get("smtp", {})
    to   = cfg.get("notify_email", "")

    if not to or not smtp.get("host"):
        log.info("Email not configured — skipping notification")
        return

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = smtp.get("from", smtp.get("username", ""))
        msg["To"]      = to

        with smtplib.SMTP(smtp["host"], int(smtp.get("port", 587))) as s:
            s.starttls()
            if smtp.get("username") and smtp.get("password"):
                s.login(smtp["username"], smtp["password"])
            s.send_message(msg)
        log.info("Email sent to {}".format(to))
    except Exception as e:
        log.error("Email failed: {}".format(e))

# ---------------------------------------------------------------------------
# Write status.json for dashboard
# ---------------------------------------------------------------------------

def write_status(status):
    with open(str(STATUS_FILE), "w") as f:
        json.dump(status, f, indent=2, default=str)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()

    tz           = ZoneInfo("Europe/Stockholm")
    now          = datetime.now(tz)
    current_hour = now.hour

    log.info("Agent tick — {}".format(now.strftime("%Y-%m-%d %H:%M")))

    from datetime import timedelta

    # 1. Fetch today's data
    tomorrow     = now + timedelta(days=1)
    prices_today = fetch_spot_prices(cfg["price_zone"], now)
    prices_tmrw  = fetch_spot_prices(cfg["price_zone"], tomorrow)
    solar_48h    = fetch_solar_forecast(cfg["latitude"], cfg["longitude"], now)

    if not prices_today:
        log.error("No price data — aborting")
        return

    # Split solar into today/tomorrow
    today_str    = now.strftime("%Y-%m-%d")
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")
    solar_today  = [s for s in solar_48h if s.get("date") == today_str]
    solar_tmrw   = [s for s in solar_48h if s.get("date") == tomorrow_str]

    # 2. Compute schedules
    schedule_today = compute_schedule_for_day(cfg, prices_today, solar_today)
    schedule_tmrw  = compute_schedule_for_day(cfg, prices_tmrw,  solar_tmrw) if prices_tmrw else []

    slot = next((s for s in schedule_today if s["hour"] == current_hour), None)

    if not slot:
        log.error("No schedule slot for hour {}".format(current_hour))
        return

    mode    = slot["mode"]
    command = "Charge" if mode == "grid_charge" else "Stop"
    price   = slot["price_SEK"]
    ghi     = slot["ghi_W_m2"]

    log.info("Hour {}: {} -> {} | {}".format(
        current_hour, mode, command, slot["reason"]))

    # 3. Set battery
    dry_run = cfg.get("dry_run", False)
    result  = set_battery_command(cfg["isolarcloud"], command, dry_run=dry_run)

    # 4. Write status.json (includes both days for dashboard)
    status = {
        "last_run":       now.isoformat(),
        "current_hour":   current_hour,
        "today":          today_str,
        "tomorrow":       tomorrow_str,
        "mode":           mode,
        "command":        command,
        "reason":         slot["reason"],
        "price_SEK":      price,
        "price_ore":      round(price * 100, 1) if price else None,
        "ghi_W_m2":       ghi,
        "result":         result,
        "schedule":       schedule_today,
        "schedule_tmrw":  schedule_tmrw,
        "dry_run":        dry_run,
    }
    write_status(status)

    # 5. Send email notification
    if result.get("status") in ("ok", "dry_run"):
        price_str = "{:.1f} öre/kWh".format(price*100) if price else "unknown"
        subject = "Battery Agent: {} at {:02d}:00 ({})".format(
            command, current_hour, price_str)
        body = (
            "Solar Battery Agent Report\n"
            "==========================\n"
            "Time:     {:02d}:00\n"
            "Mode:     {}\n"
            "Command:  {}\n"
            "Reason:   {}\n"
            "Price:    {}\n"
            "Solar GHI:{} W/m2\n"
            "Status:   {}\n"
            "\nToday's cheapest hours:\n"
        ).format(
            current_hour, mode, command, slot["reason"],
            price_str, ghi, result.get("status")
        )
        # Add cheap hours summary
        cheap = [s for s in schedule if s["mode"] == "grid_charge"]
        for s in cheap:
            body += "  {:02d}:00 — {:.1f} öre\n".format(
                s["hour"], s["price_SEK"]*100 if s["price_SEK"] else 0)

        send_email(cfg, subject, body)
    else:
        send_email(
            cfg,
            "Battery Agent ERROR at {:02d}:00".format(current_hour),
            "Error: {}\nCheck logs at ~/solar-agent/logs/agent.log".format(
                result.get("msg"))
        )

    log.info("Done.")


if __name__ == "__main__":
    main()
