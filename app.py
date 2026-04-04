"""
Solar Battery Agent
===================
Fetches SE4 spot prices (Nordpool via elprisetjustnu.se) + solar radiation
forecast (Open-Meteo), then sets iSolarCloud battery charging parameters.

Rules:
  1. During solar hours (GHI >= threshold) -> Stop (panels charge battery)
  2. During cheapest N non-solar hours     -> Charge (force grid charging)
  3. All other hours                       -> Stop (self-consumption)

Deployment: Phusion Passenger WSGI (simply.com / Python 3.6)
Scheduling: simply.com URL cron -> hits /cron/run every hour
iSolarCloud: Direct OAuth2 HTTP calls (no pysolarcloud library needed)
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE  = BASE_DIR / "state.json"
TOKEN_FILE  = BASE_DIR / "token.json"
LOG_FILE    = BASE_DIR / "logs" / "agent.log"
LOG_FILE.parent.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "latitude":            55.702,
    "longitude":           13.163,
    "price_zone":          "SE4",
    "cheap_hours_per_day": 3,
    "solar_ghi_threshold": 100,
    "cron_secret":         "",
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
# Config helpers
# ---------------------------------------------------------------------------

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
        return merged
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(str(CONFIG_FILE), "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            with open(str(STATE_FILE)) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "last_run": None,
        "schedule": [],
        "current_hour_mode": None,
        "last_action": None,
        "last_action_result": None,
        "log_entries": []
    }


def save_state(state):
    with open(str(STATE_FILE), "w") as f:
        json.dump(state, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def load_token():
    if TOKEN_FILE.exists():
        try:
            with open(str(TOKEN_FILE)) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_token(tokens):
    with open(str(TOKEN_FILE), "w") as f:
        json.dump(tokens, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
state_lock  = threading.Lock()
agent_state = load_state()


def add_log(msg, level="info"):
    entry = {"time": datetime.now().isoformat(), "level": level, "msg": msg}
    with state_lock:
        agent_state["log_entries"].insert(0, entry)
        agent_state["log_entries"] = agent_state["log_entries"][:200]
    getattr(log, level, log.info)(msg)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_spot_prices(zone, date):
    """Fetch Nordpool spot prices from elprisetjustnu.se. Free, no key."""
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

        prices = [
            {
                "hour": h,
                "SEK_per_kWh": round(sum(v) / len(v), 4),
            }
            for h, v in sorted(hour_prices.items())
        ]
        log.info("Fetched {} price points for {}".format(len(prices), zone))
        return prices
    except Exception as e:
        log.error("Failed to fetch spot prices: {}".format(e))
        return []


def fetch_solar_forecast(lat, lon, date):
    """Fetch hourly GHI (W/m2) from Open-Meteo. Free, no key."""
    date_str = date.strftime("%Y-%m-%d")
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude={}&longitude={}"
        "&hourly=shortwave_radiation"
        "&start_date={}&end_date={}"
        "&timezone=Europe/Stockholm"
    ).format(lat, lon, date_str, date_str)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        times = data["hourly"]["time"]
        ghi   = data["hourly"]["shortwave_radiation"]
        result = [
            {"hour": datetime.fromisoformat(t).hour, "ghi": round(g or 0, 1)}
            for t, g in zip(times, ghi)
        ]
        peak = max(h["ghi"] for h in result)
        log.info("Solar forecast: peak GHI {:.0f} W/m2".format(peak))
        return result
    except Exception as e:
        log.error("Failed to fetch solar forecast: {}".format(e))
        return []


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def compute_schedule(cfg, prices, solar):
    threshold = cfg["solar_ghi_threshold"]
    n_cheap   = cfg["cheap_hours_per_day"]

    ghi_by_hour   = {h["hour"]: h["ghi"] for h in solar}
    price_by_hour = {p["hour"]: p["SEK_per_kWh"] for p in prices}

    solar_hours = {h for h, g in ghi_by_hour.items() if g >= threshold}

    non_solar_prices = sorted(
        [(h, price_by_hour[h]) for h in price_by_hour if h not in solar_hours],
        key=lambda x: x[1]
    )
    cheap_hours = {h for h, _ in non_solar_prices[:n_cheap]}
    cheap_rank  = {h: i + 1 for i, (h, _) in enumerate(non_solar_prices)}

    schedule = []
    for hour in range(24):
        price = price_by_hour.get(hour)
        ghi   = ghi_by_hour.get(hour, 0)

        if hour in solar_hours:
            mode   = "solar"
            reason = "Solar generating ({:.0f} W/m2 >= {}) -- self-consumption".format(
                ghi, threshold)
        elif hour in cheap_hours:
            mode   = "grid_charge"
            reason = "Cheapest non-solar hour (rank #{}) -- force grid charge".format(
                cheap_rank.get(hour, "?"))
        else:
            mode   = "normal"
            reason = "Self-consumption / idle"

        schedule.append({
            "hour":      hour,
            "price_SEK": round(price, 4) if price is not None else None,
            "ghi_W_m2":  ghi,
            "mode":      mode,
            "reason":    reason,
        })

    return schedule


# ---------------------------------------------------------------------------
# iSolarCloud — direct OAuth2 HTTP calls (no external library)
# ---------------------------------------------------------------------------

def isolarcloud_headers(app_key, app_secret, access_token=None):
    """Build request headers for iSolarCloud API calls."""
    h = {
        "Content-Type":    "application/json",
        "x-access-key":    app_key,
        "x-access-secret": app_secret,
    }
    if access_token:
        h["Authorization"] = "Bearer {}".format(access_token)
    return h


def exchange_code_for_token(iso_cfg, code):
    """Exchange OAuth2 authorization code for access + refresh tokens."""
    client_id = "{}@{}".format(iso_cfg["app_id"], iso_cfg["app_key"])
    resp = requests.post(
        "{}/openapi/platform/oauth2/token".format(ISOLARCLOUD_BASE),
        headers=isolarcloud_headers(iso_cfg["app_key"], iso_cfg["app_secret"]),
        json={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  iso_cfg["redirect_uri"],
            "client_id":     client_id,
            "client_secret": iso_cfg["app_secret"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    log.info("Token exchange response: {}".format(data))
    return data


def refresh_access_token(iso_cfg, refresh_token):
    """Use refresh token to get a new access token."""
    client_id = "{}@{}".format(iso_cfg["app_id"], iso_cfg["app_key"])
    resp = requests.post(
        "{}/openapi/platform/oauth2/token".format(ISOLARCLOUD_BASE),
        headers=isolarcloud_headers(iso_cfg["app_key"], iso_cfg["app_secret"]),
        json={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     client_id,
            "client_secret": iso_cfg["app_secret"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_valid_token(iso_cfg):
    """
    Return a valid access token, refreshing if needed.
    Saves updated token back to disk.
    Returns None if no token available.
    """
    token = load_token()
    if not token:
        return None

    # Check expiry (with 60s buffer)
    expires_at = token.get("expires_at", 0)
    now        = int(datetime.now(timezone.utc).timestamp())

    if expires_at - now < 60:
        log.info("Access token expired — refreshing...")
        try:
            new_token = refresh_access_token(iso_cfg, token["refresh_token"])
            # Merge and save
            token.update(new_token)
            if "expires_in" in new_token:
                token["expires_at"] = now + int(new_token["expires_in"]) - 20
            save_token(token)
            log.info("Token refreshed successfully")
        except Exception as e:
            log.error("Token refresh failed: {}".format(e))
            return None

    return token.get("access_token")


def set_battery_command(iso_cfg, command):
    """
    Set the battery charge/discharge command directly via iSolarCloud API.

    command: 'Charge' | 'Stop' | 'Discharge'

    Uses the same endpoint pysolarcloud uses, but with direct HTTP calls.
    Verified working with device_uuid=4033562 (SH10RT-V112).
    """
    if not iso_cfg.get("app_key") or not iso_cfg.get("app_secret"):
        log.warning("iSolarCloud not configured -- DRY RUN")
        return {"status": "dry_run", "command": command}

    access_token = get_valid_token(iso_cfg)
    if not access_token:
        log.error("No valid token -- re-authorize at /auth/start")
        return {"status": "error", "msg": "no valid token"}

    # Parameter codes discovered from pysolarcloud inspection:
    # charge_discharge_command -> code 10004
    # Values: Charge=170, Discharge=187, Stop=204
    value_map = {"Charge": "170", "Discharge": "187", "Stop": "204"}
    set_value = value_map.get(command, "204")

    payload = {
        "set_type":      0,
        "uuid":          str(iso_cfg["device_uuid"]),
        "task_name":     "BatteryAgent {}".format(
            datetime.now().strftime("%Y-%m-%d %H:%M")),
        "expire_second": 120,
        "param_list": [
            {"param_code": "10004", "set_value": set_value}
        ],
    }

    try:
        resp = requests.post(
            "{}/openapi/platform/paramSetting".format(ISOLARCLOUD_BASE),
            headers=isolarcloud_headers(
                iso_cfg["app_key"], iso_cfg["app_secret"], access_token),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        log.info("set_battery_command={} response={}".format(command, data))
        return {"status": "ok", "command": command, "response": data}
    except Exception as e:
        log.error("set_battery_command failed: {}".format(e))
        return {"status": "error", "msg": str(e)}


# ---------------------------------------------------------------------------
# Core agent
# ---------------------------------------------------------------------------
_agent_lock = threading.Lock()


def run_agent(cfg=None, manual=False):
    """
    Core tick. Called from:
      - GET /cron/run  (simply.com URL cron, every hour)
      - POST /api/run  (web UI Run Now button)
    """
    if not _agent_lock.acquire(blocking=False):
        add_log("Agent already running -- skipped", "warning")
        return
    try:
        if cfg is None:
            cfg = load_config()

        tz           = ZoneInfo("Europe/Stockholm")
        now          = datetime.now(tz)
        current_hour = now.hour

        add_log("{}Agent tick -- {}".format(
            "[MANUAL] " if manual else "",
            now.strftime("%Y-%m-%d %H:%M")))

        # 1. Fetch data
        prices = fetch_spot_prices(cfg["price_zone"], now)
        solar  = fetch_solar_forecast(cfg["latitude"], cfg["longitude"], now)

        if not prices:
            add_log("No price data -- aborting", "warning")
            return

        # 2. Compute schedule
        schedule = compute_schedule(cfg, prices, solar)
        slot     = next((s for s in schedule if s["hour"] == current_hour),
                        None)

        with state_lock:
            agent_state["last_run"] = now.isoformat()
            agent_state["schedule"] = schedule

        if not slot:
            add_log("No slot for hour {}".format(current_hour), "warning")
            return

        mode = slot["mode"]
        add_log("Hour {}: {} -- {}".format(current_hour, mode, slot["reason"]))

        # 3. Map mode to battery command
        command = "Charge" if mode == "grid_charge" else "Stop"

        # 4. Apply to battery
        result = set_battery_command(cfg["isolarcloud"], command)

        with state_lock:
            agent_state["current_hour_mode"] = mode
            agent_state["last_action"]        = "charge_discharge_command={}".format(command)
            agent_state["last_action_result"] = result

        if result.get("status") == "ok":
            add_log("Battery set to: {}".format(command))
        elif result.get("status") == "dry_run":
            add_log("DRY RUN -- iSolarCloud not configured")
        else:
            add_log("Battery control failed: {}".format(
                result.get("msg")), "error")

        # 5. Persist state
        with state_lock:
            save_state(agent_state)

    finally:
        _agent_lock.release()


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/cron/run")
def cron_run():
    """Called by simply.com URL cron every hour."""
    cfg    = load_config()
    secret = cfg.get("cron_secret", "")
    if secret and request.args.get("secret") != secret:
        abort(403)
    threading.Thread(target=run_agent, daemon=True).start()
    return "OK", 200


@app.route("/auth/start")
def auth_start():
    """Redirect browser to iSolarCloud authorization page."""
    cfg = load_config()
    iso = cfg["isolarcloud"]
    url = (
        "https://web3.isolarcloud.eu/#/authorized-app"
        "?cloudId=3"
        "&applicationId={}".format(iso["app_id"]) +
        "&redirectUrl={}".format(iso["redirect_uri"])
    )
    return redirect(url)


@app.route("/auth/callback")
def auth_callback():
    """
    OAuth2 redirect handler.
    iSolarCloud sends user here with ?code=XXX after approval.
    We exchange the code for a token and save it.
    """
    code = request.args.get("code")
    if not code:
        return "Missing code parameter", 400

    cfg = load_config()
    iso = cfg["isolarcloud"]

    if not iso.get("app_key") or not iso.get("app_secret"):
        return "iSolarCloud credentials not configured in config.json", 500

    try:
        data = exchange_code_for_token(iso, code)

        # Build token dict with expiry
        now = int(datetime.now(timezone.utc).timestamp())
        token = {
            "access_token":  data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_at":    now + int(data.get("expires_in", 172799)) - 20,
            "raw":           data,
        }
        save_token(token)
        add_log("OAuth2 token obtained and saved via /auth/callback")

        return (
            "<h2>Authorization successful!</h2>"
            "<p>Token saved. The agent is now authorized.</p>"
            "<p><a href='/'>Go to dashboard</a></p>"
        )
    except Exception as e:
        log.error("Auth callback failed: {}".format(e))
        return "<h2>Authorization failed: {}</h2>".format(e), 500


@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify(agent_state)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg  = load_config()
    safe = dict(cfg)
    iso  = dict(safe["isolarcloud"])
    if iso.get("app_secret"):
        iso["app_secret"] = "........"
    safe["isolarcloud"] = iso
    if safe.get("cron_secret"):
        safe["cron_secret"] = "........"
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_config_post():
    cfg  = load_config()
    data = request.json
    for key in ["latitude", "longitude", "price_zone",
                "cheap_hours_per_day", "solar_ghi_threshold"]:
        if key in data:
            cfg[key] = data[key]
    if "cron_secret" in data and data["cron_secret"] != "........":
        cfg["cron_secret"] = data["cron_secret"]
    if "isolarcloud" in data:
        for k, v in data["isolarcloud"].items():
            if k == "app_secret" and v == "........":
                continue
            cfg["isolarcloud"][k] = v
    save_config(cfg)
    add_log("Configuration saved via web UI")
    return jsonify({"status": "saved"})


@app.route("/api/run", methods=["POST"])
def api_run():
    threading.Thread(
        target=run_agent, kwargs={"manual": True}, daemon=True).start()
    return jsonify({"status": "triggered"})


@app.route("/api/token/status")
def api_token_status():
    token = load_token()
    if not token:
        return jsonify({"status": "missing"})
    expires_at = token.get("expires_at", 0)
    now        = int(datetime.now(timezone.utc).timestamp())
    return jsonify({
        "status":     "valid" if expires_at > now else "expired",
        "expires_at": expires_at,
        "expires_in": max(0, expires_at - now),
    })


@app.route("/api/logs")
def api_logs():
    with state_lock:
        return jsonify(agent_state.get("log_entries", []))


# Passenger WSGI entry point
application = app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
