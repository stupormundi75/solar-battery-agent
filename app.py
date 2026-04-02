"""
Solar Battery Agent
===================
Fetches SE4 spot prices (Nordpool via elprisetjustnu.se) + solar radiation
forecast (Open-Meteo), then sets iSolarCloud battery charging parameters.

Rules:
  1. During solar hours (GHI >= threshold) → Stop (panels charge battery)
  2. During cheapest N non-solar hours    → Charge (force grid charging)
  3. All other hours                      → Stop (self-consumption)

Deployment: Phusion Passenger WSGI (simply.com)
Scheduling: simply.com URL cron → hits /cron/run every hour
State:      Persisted to state.json so dashboard survives Passenger restarts
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests as http_requests
from flask import Flask, abort, jsonify, render_template, request

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
    "latitude":            55.6,
    "longitude":           13.0,
    "price_zone":          "SE4",
    "cheap_hours_per_day": 3,
    "solar_ghi_threshold": 100,
    "cron_secret":         "",
    "isolarcloud": {
        "app_key":     "",
        "app_secret":  "",
        "app_id":      "3251",
        "plant_id":    "5486815",
        "device_uuid": "4033562",
        "redirect_uri": "https://battery.godaly.com/auth/callback",
    }
}

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

def load_config() -> dict:
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


def save_config(cfg: dict):
    with open(str(CONFIG_FILE), "w") as f:
        json.dump(cfg, f, indent=2)

# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def load_state() -> dict:
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


def save_state(state: dict):
    with open(str(STATE_FILE), "w") as f:
        json.dump(state, f, indent=2, default=str)

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def load_token() -> dict | None:
    if TOKEN_FILE.exists():
        try:
            with open(str(TOKEN_FILE)) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_token(tokens: dict):
    with open(str(TOKEN_FILE), "w") as f:
        json.dump(tokens, f, indent=2, default=str)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
state_lock  = threading.Lock()
agent_state = load_state()


def add_log(msg: str, level: str = "info"):
    entry = {"time": datetime.now().isoformat(), "level": level, "msg": msg}
    with state_lock:
        agent_state["log_entries"].insert(0, entry)
        agent_state["log_entries"] = agent_state["log_entries"][:200]
    getattr(log, level, log.info)(msg)

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_spot_prices(zone: str, date: datetime) -> list:
    """Fetch Nordpool spot prices from elprisetjustnu.se. Free, no key."""
    url = (
        "https://www.elprisetjustnu.se/api/v1/prices/"
        "{}/{:02d}-{:02d}_{}.json".format(
            date.year, date.month, date.day, zone)
    )
    try:
        r = http_requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        # Average per hour (API returns 96 x 15-min slots since Oct 2025)
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


def fetch_solar_forecast(lat: float, lon: float, date: datetime) -> list:
    """Fetch hourly GHI (W/m²) from Open-Meteo. Free, no key."""
    date_str = date.strftime("%Y-%m-%d")
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude={}&longitude={}"
        "&hourly=shortwave_radiation"
        "&start_date={}&end_date={}"
        "&timezone=Europe/Stockholm"
    ).format(lat, lon, date_str, date_str)
    try:
        r = http_requests.get(url, timeout=10)
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

def compute_schedule(cfg: dict, prices: list, solar: list) -> list:
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
            reason = "Solar generating ({:.0f} W/m2 >= {}) — self-consumption".format(
                ghi, threshold)
        elif hour in cheap_hours:
            mode   = "grid_charge"
            reason = "Cheapest non-solar hour (rank #{}) — force grid charge".format(
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
# iSolarCloud controller (uses pysolarcloud + OAuth2)
# ---------------------------------------------------------------------------

async def set_battery_mode_async(isolar_cfg: dict, mode: str) -> dict:
    """
    Set battery charging mode via pysolarcloud.

    mode: 'grid_charge' → Charge from grid
          'solar'        → Stop (let solar do it)
          'normal'       → Stop (self-consumption)
    """
    try:
        from pysolarcloud import Auth, Server
        from pysolarcloud.control import Control
    except ImportError:
        log.error("pysolarcloud not installed")
        return {"status": "error", "msg": "pysolarcloud not installed"}

    # Check credentials
    if not isolar_cfg.get("app_key") or not isolar_cfg.get("app_secret"):
        log.warning("iSolarCloud not configured — DRY RUN")
        return {"status": "dry_run", "mode": mode}

    # Check token
    token = load_token()
    if not token:
        log.error("No OAuth2 token found — run authorization first")
        return {"status": "error", "msg": "no token — re-authorize"}

    # Map mode to command
    command = "Charge" if mode == "grid_charge" else "Stop"

    try:
        auth = Auth(Server.Europe,
                    isolar_cfg["app_key"],
                    isolar_cfg["app_secret"],
                    isolar_cfg["app_id"])
        auth.tokens = token

        control     = Control(auth)
        device_uuid = int(isolar_cfg["device_uuid"])

        result = await control.async_update_parameters(
            device_uuid,
            [("charge_discharge_command", command)]
        )

        # Save refreshed token if it changed
        if auth.tokens != token:
            save_token(auth.tokens)

        log.info("iSolarCloud set charge_discharge_command={} result={}".format(
            command, result))
        return {"status": "ok", "command": command, "result": result}

    except Exception as e:
        log.error("iSolarCloud control failed: {}".format(e))
        return {"status": "error", "msg": str(e)}


def set_battery_mode(isolar_cfg: dict, mode: str) -> dict:
    """Synchronous wrapper for use from Flask routes."""
    return asyncio.run(set_battery_mode_async(isolar_cfg, mode))

# ---------------------------------------------------------------------------
# Core agent
# ---------------------------------------------------------------------------
_agent_lock = threading.Lock()


def run_agent(cfg: dict = None, manual: bool = False):
    """
    Core tick. Called from:
      - GET /cron/run  (simply.com URL cron, every hour)
      - POST /api/run  (web UI Run Now button)
    """
    if not _agent_lock.acquire(blocking=False):
        add_log("Agent already running — skipped", "warning")
        return
    try:
        if cfg is None:
            cfg = load_config()

        tz           = ZoneInfo("Europe/Stockholm")
        now          = datetime.now(tz)
        current_hour = now.hour

        add_log("{}Agent tick — {}".format(
            "[MANUAL] " if manual else "",
            now.strftime("%Y-%m-%d %H:%M")))

        # 1. Fetch data
        prices = fetch_spot_prices(cfg["price_zone"], now)
        solar  = fetch_solar_forecast(cfg["latitude"], cfg["longitude"], now)

        if not prices:
            add_log("No price data — aborting", "warning")
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
        add_log("Hour {}: {} — {}".format(current_hour, mode, slot["reason"]))

        # 3. Apply to battery
        result = set_battery_mode(cfg["isolarcloud"], mode)

        with state_lock:
            agent_state["current_hour_mode"] = mode
            agent_state["last_action"]        = "charge_discharge_command={}".format(
                "Charge" if mode == "grid_charge" else "Stop")
            agent_state["last_action_result"] = result

        if result.get("status") == "ok":
            add_log("✓ Battery set to: {}".format(result.get("command")))
        elif result.get("status") == "dry_run":
            add_log("~ DRY RUN — iSolarCloud not configured")
        else:
            add_log("✗ Battery control failed: {}".format(
                result.get("msg")), "error")

        # 4. Persist state
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


@app.route("/auth/callback")
def auth_callback():
    """
    OAuth2 redirect handler.
    After user approves on iSolarCloud, they are redirected here with ?code=XXX
    We exchange the code for a token and save it.
    """
    code = request.args.get("code")
    if not code:
        return "Missing code parameter", 400

    cfg = load_config()
    iso = cfg["isolarcloud"]

    async def do_exchange():
        from pysolarcloud import Auth, Server
        auth = Auth(Server.Europe, iso["app_key"], iso["app_secret"],
                    iso["app_id"])
        await auth.async_authorize(code, iso["redirect_uri"])
        save_token(auth.tokens)
        return auth.tokens

    try:
        tokens = asyncio.run(do_exchange())
        add_log("OAuth2 token obtained and saved via /auth/callback")
        return (
            "<h2>✅ Authorization successful!</h2>"
            "<p>Token saved. You can close this window.</p>"
            "<p><a href='/'>Go to dashboard</a></p>"
        )
    except Exception as e:
        log.error("Auth callback failed: {}".format(e))
        return "<h2>❌ Authorization failed: {}</h2>".format(e), 500


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
    from flask import redirect
    return redirect(url)


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
        iso["app_secret"] = "••••••••"
    safe["isolarcloud"] = iso
    if safe.get("cron_secret"):
        safe["cron_secret"] = "••••••••"
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_config_post():
    cfg  = load_config()
    data = request.json
    for key in ["latitude", "longitude", "price_zone",
                "cheap_hours_per_day", "solar_ghi_threshold"]:
        if key in data:
            cfg[key] = data[key]
    if "cron_secret" in data and data["cron_secret"] != "••••••••":
        cfg["cron_secret"] = data["cron_secret"]
    if "isolarcloud" in data:
        for k, v in data["isolarcloud"].items():
            if k == "app_secret" and v == "••••••••":
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
