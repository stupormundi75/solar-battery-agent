"""
Solar Battery Agent — Python 3.6 compatible
============================================
Fetches SE4 spot prices (Nordpool via elprisetjustnu.se) + solar radiation
forecast (Open-Meteo), then sets iSolarCloud battery charging parameters.

Rules:
  1. During solar hours (GHI >= threshold) → NO grid charging (panels do it)
  2. During cheapest N non-solar hours → FORCE charge from grid
  3. All other hours → normal self-consumption mode

Deployment: Phusion Passenger WSGI (simply.com)
Scheduling: simply.com URL cron → hits /cron/run every hour
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, render_template, request

# Python 3.6 compatibility: use backports.zoneinfo instead of zoneinfo
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Paths — all relative to this file so Passenger finds them correctly
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE  = BASE_DIR / "state.json"
LOG_FILE    = BASE_DIR / "logs" / "agent.log"
LOG_FILE.parent.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "latitude": 55.6,
    "longitude": 13.0,
    "price_zone": "SE4",
    "cheap_hours_per_day": 3,
    "solar_ghi_threshold": 100,
    "battery_charge_power": 3000,
    "cron_secret": "",
    "isolarcloud": {
        "username": "",
        "password": "",
        "app_key": "",
        "app_secret": "",
        "plant_id": ""
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
# Persistent state (survives Passenger process restarts)
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
# In-memory state — loaded from disk at startup
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
    """
    Fetch spot prices from elprisetjustnu.se (Nordpool data, free, no key).
    Since Oct 2025 prices are per 15-min (96/day) — averaged to hourly here.
    """
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
                "time_start": "{}-{:02d}-{:02d}T{:02d}:00:00".format(
                    date.year, date.month, date.day, h),
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
    """Fetch hourly GHI (W/m²) from Open-Meteo. Free, no API key."""
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
        log.info("Solar forecast fetched: peak GHI {:.0f} W/m2".format(peak))
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
            reason = "Solar generating ({:.0f} W/m2 >= {}) — no grid charge".format(
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
# iSolarCloud controller
# ---------------------------------------------------------------------------

class ISolarCloudController:
    """
    Wrapper around the iSolarCloud Open API (Sungrow).

    Credentials setup:
    1. Register at https://developer-api.isolarcloud.com
    2. Applications → Create (choose WITHOUT OAuth2)
    3. Wait ~1-2 days for approval
    4. Action → View to get app_key and app_secret
    5. Find plant_id (ps_id) from URL on web3.isolarcloud.eu
    """
    BASE_URL = "https://gateway.isolarcloud.eu"

    def __init__(self, cfg):
        self.cfg            = cfg["isolarcloud"]
        self._token         = None
        self._token_expires = None

    def _is_configured(self):
        return all([self.cfg.get(k) for k in
                    ("username", "password", "app_key", "plant_id")])

    def _ensure_token(self):
        now = datetime.now(timezone.utc)
        if self._token and self._token_expires and now < self._token_expires:
            return
        log.info("Refreshing iSolarCloud token...")
        resp = requests.post(
            "{}/v1/userService/login".format(self.BASE_URL),
            json={
                "user_account":  self.cfg["username"],
                "user_password": self.cfg["password"],
                "appkey":        self.cfg["app_key"],
                "sys_code":      "901",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("result_code") != "1":
            raise RuntimeError("iSolarCloud login failed: {}".format(
                data.get("result_msg")))
        self._token         = data["result_data"]["token"]
        self._token_expires = now + timedelta(hours=23)
        log.info("iSolarCloud token refreshed")

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "token":        self._token,
            "sys_code":     "901",
            "appkey":       self.cfg["app_key"],
        }

    def set_charging_mode(self, mode, charge_power_w=3000):
        """
        mode: 'self_consumption' | 'forced_charge' | 'forced_discharge'
        NOTE: ems_mode codes may vary by inverter model — verify in
              the iSolarCloud developer portal for your specific device.
        """
        if not self._is_configured():
            log.warning("iSolarCloud not configured — DRY RUN")
            return {"status": "dry_run", "mode": mode}

        self._ensure_token()

        mode_map = {
            "self_consumption": {"ems_mode": "0"},
            "forced_charge":    {"ems_mode": "2",
                                 "charge_power": str(charge_power_w)},
            "forced_discharge": {"ems_mode": "3"},
        }
        payload = {"ps_id": self.cfg["plant_id"]}
        payload.update(mode_map.get(mode, {"ems_mode": "0"}))

        resp = requests.post(
            "{}/v1/devService/setDevParam".format(self.BASE_URL),
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        log.info("iSolarCloud set_charging_mode '{}': {}".format(mode, result))
        return result

    def get_current_status(self):
        if not self._is_configured():
            return {"status": "not_configured"}
        try:
            self._ensure_token()
            resp = requests.post(
                "{}/v1/devService/getDevRealKpiData".format(self.BASE_URL),
                headers=self._headers(),
                json={"ps_id": self.cfg["plant_id"]},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# Core agent
# ---------------------------------------------------------------------------
_agent_lock = threading.Lock()


def run_agent(cfg=None, manual=False):
    """
    Core tick. Called from:
      - GET /cron/run  (simply.com URL cron, every hour)
      - POST /api/run  (web UI "Run Now" button)
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

        prices = fetch_spot_prices(cfg["price_zone"], now)
        solar  = fetch_solar_forecast(cfg["latitude"], cfg["longitude"], now)

        if not prices:
            add_log("No price data — aborting run", "warning")
            return

        schedule = compute_schedule(cfg, prices, solar)
        slot     = next((s for s in schedule if s["hour"] == current_hour),
                        None)

        with state_lock:
            agent_state["last_run"] = now.isoformat()
            agent_state["schedule"] = schedule

        if not slot:
            add_log("No schedule slot for hour {}".format(current_hour),
                    "warning")
            return

        mode = slot["mode"]
        add_log("Hour {}: {} — {}".format(current_hour, mode, slot["reason"]))

        isolar_mode = {
            "solar":       "self_consumption",
            "normal":      "self_consumption",
            "grid_charge": "forced_charge",
        }.get(mode, "self_consumption")

        controller = ISolarCloudController(cfg)
        try:
            result = controller.set_charging_mode(
                isolar_mode,
                charge_power_w=cfg.get("battery_charge_power", 3000)
            )
            with state_lock:
                agent_state["current_hour_mode"] = mode
                agent_state["last_action"]        = \
                    "iSolarCloud → {}".format(isolar_mode)
                agent_state["last_action_result"] = result
            add_log("✓ iSolarCloud → {}".format(isolar_mode))
        except Exception as e:
            add_log("✗ iSolarCloud failed: {}".format(e), "error")

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


@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify(agent_state)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg  = load_config()
    safe = dict(cfg)
    if safe["isolarcloud"].get("password"):
        safe["isolarcloud"]["password"] = "••••••••"
    if safe.get("cron_secret"):
        safe["cron_secret"] = "••••••••"
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_config_post():
    cfg  = load_config()
    data = request.json
    for key in ["latitude", "longitude", "price_zone",
                "cheap_hours_per_day", "solar_ghi_threshold",
                "battery_charge_power"]:
        if key in data:
            cfg[key] = data[key]
    if "cron_secret" in data and data["cron_secret"] != "••••••••":
        cfg["cron_secret"] = data["cron_secret"]
    if "isolarcloud" in data:
        for k, v in data["isolarcloud"].items():
            if k == "password" and v == "••••••••":
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


@app.route("/api/logs")
def api_logs():
    with state_lock:
        return jsonify(agent_state.get("log_entries", []))


# Passenger WSGI entry point — imported by passenger_wsgi.py
application = app

if __name__ == "__main__":
    # Local development only — not used by Passenger
    app.run(host="0.0.0.0", port=5000, debug=True)
