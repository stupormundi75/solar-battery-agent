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

iSolarCloud auth (from pysolarcloud source inspection):
  - x-access-key header = APP_SECRET
  - appkey in body      = APP_KEY
  - Token endpoint      = /openapi/apiManage/token
  - Refresh endpoint    = /openapi/apiManage/refreshToken
  - Control endpoint    = /openapi/platform/paramSetting
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


def save_token(token):
    with open(str(TOKEN_FILE), "w") as f:
        json.dump(token, f, indent=2, default=str)


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
# iSolarCloud HTTP helpers
# (reverse-engineered from pysolarcloud source)
# ---------------------------------------------------------------------------

def isolar_headers(app_secret, access_token=None):
    """
    x-access-key = APP_SECRET  (pysolarcloud: self.access_key = client_secret)
    Authorization = Bearer {token}
    """
    h = {
        "Content-Type": "application/json",
        "x-access-key":  app_secret,
    }
    if access_token:
        h["Authorization"] = "Bearer {}".format(access_token)
    return h


def isolar_body(data, app_key):
    """
    appkey in body = APP_KEY  (pysolarcloud: self.appkey = client_id)
    lang = _en_US
    """
    body = dict(data)
    body["appkey"] = app_key
    body["lang"]   = "_en_US"
    return body


def isolar_post(path, data, iso_cfg, access_token=None):
    """Make a request to iSolarCloud matching pysolarcloud exactly."""
    resp = requests.post(
        "{}{}".format(ISOLARCLOUD_BASE, path),
        headers=isolar_headers(iso_cfg["app_secret"], access_token),
        json=isolar_body(data, iso_cfg["app_key"]),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_valid_token(iso_cfg):
    """Return a valid access token, refreshing if needed."""
    token = load_token()
    if not token:
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
                log.info("Token refreshed successfully")
            else:
                log.error("Token refresh failed: {}".format(data))
                return None
        except Exception as e:
            log.error("Token refresh error: {}".format(e))
            return None

    return token.get("access_token")


def exchange_code_for_token(iso_cfg, code):
    """Exchange OAuth2 authorization code for tokens."""
    data = isolar_post(
        "/openapi/apiManage/token",
        {
            "code":         code,
            "grant_type":   "authorization_code",
            "redirect_uri": iso_cfg["redirect_uri"],
        },
        iso_cfg,
    )
    log.info("Token exchange response: {}".format(data))
    return data


def set_battery_command(iso_cfg, command):
    """
    Set battery charge/discharge command.
    command: 'Charge' | 'Stop' | 'Discharge'
    Verified working with SH10RT-V112 (device_uuid=4033562).
    param_code 10004 = charge_discharge_command
    Values: Charge=170, Stop=204, Discharge=187
    """
    if not iso_cfg.get("app_key") or not iso_cfg.get("app_secret"):
        log.warning("iSolarCloud not configured -- DRY RUN")
        return {"status": "dry_run", "command": command}

    access_token = get_valid_token(iso_cfg)
    if not access_token:
        log.error("No valid token -- visit /auth/start to re-authorize")
        return {"status": "error", "msg": "no valid token"}

    value_map = {"Charge": "170", "Discharge": "187", "Stop": "204"}
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
# Core agent
# ---------------------------------------------------------------------------
_agent_lock = threading.Lock()


def run_agent(cfg=None, manual=False):
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

        prices = fetch_spot_prices(cfg["price_zone"], now)
        solar  = fetch_solar_forecast(cfg["latitude"], cfg["longitude"], now)

        if not prices:
            add_log("No price data -- aborting", "warning")
            return

        schedule = compute_schedule(cfg, prices, solar)
        slot     = next((s for s in schedule if s["hour"] == current_hour),
                        None)

        with state_lock:
            agent_state["last_run"] = now.isoformat()
            agent_state["schedule"] = schedule

        if not slot:
            add_log("No slot for hour {}".format(current_hour), "warning")
            return

        mode    = slot["mode"]
        command = "Charge" if mode == "grid_charge" else "Stop"

        add_log("Hour {}: {} -> {} | {}".format(
            current_hour, mode, command, slot["reason"]))

        result = set_battery_command(cfg["isolarcloud"], command)

        with state_lock:
            agent_state["current_hour_mode"] = mode
            agent_state["last_action"]        = "charge_discharge_command={}".format(command)
            agent_state["last_action_result"] = result

        if result.get("status") == "ok":
            add_log("Battery set to: {}".format(command))
        elif result.get("status") == "dry_run":
            add_log("DRY RUN -- credentials not configured")
        else:
            add_log("Battery control failed: {}".format(
                result.get("msg")), "error")

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
    """Redirect to iSolarCloud authorization page."""
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
    """Receive OAuth2 code and exchange for token."""
    code = request.args.get("code")
    if not code:
        return "Missing code parameter", 400

    cfg = load_config()
    iso = cfg["isolarcloud"]

    if not iso.get("app_key") or not iso.get("app_secret"):
        return "iSolarCloud not configured in config.json", 500

    try:
        data = exchange_code_for_token(iso, code)

        if "access_token" not in data:
            return "Token exchange failed: {}".format(data), 500

        now   = int(datetime.now(timezone.utc).timestamp())
        token = {
            "access_token":  data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at":    now + int(data.get("expires_in", 172799)) - 20,
        }
        save_token(token)
        add_log("OAuth2 token saved via /auth/callback")

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
