#!/opt/alt/python311/bin/python3.11
"""
Solar Battery Agent — Smart Charging Edition
=============================================
Runs every hour via cron. Fetches spot prices + solar forecast + live battery
state, decides intelligently whether to grid-charge, then sets iSolarCloud.

Smart charging logic:
  1. During solar hours (GHI >= threshold) -> Stop (panels charge for free)
  2. During non-solar hours, check if grid charge is actually needed:
       a. Fetch current battery SOC from iSolarCloud
       b. Calculate expected solar yield tomorrow
       c. Calculate expected household consumption overnight + tomorrow morning
       d. If solar will cover remaining needs -> skip grid charge
       e. If shortfall exists -> grid charge during cheapest hours
  3. Everything else -> Stop (self-consumption)

System specs (update in config.json):
  battery_capacity_kwh: 10.0
  panel_capacity_kwp:   6.0

Cron entry:
  2 * * * * /opt/alt/python311/bin/python3.11 /var/www/godaly.com/solar-agent/agent.py
"""

import json
import logging
import smtplib
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

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
STATUS_FILE = Path("/var/www/godaly.com/battery/status.json")
LOG_FILE    = BASE_DIR / "logs" / "agent.log"
LOG_FILE.parent.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "latitude":              55.702,
    "longitude":             13.163,
    "price_zone":            "SE4",
    "cheap_hours_per_day":   3,
    "solar_ghi_threshold":   100,
    "dry_run":               False,
    "notify_email":          "",
    # System specs for smart charging
    "battery_capacity_kwh":  10.0,
    "panel_capacity_kwp":    6.0,
    "panel_efficiency":      0.16,   # 16% — typical for modern panels
    "avg_consumption_kwh":   10.0,   # daily household consumption estimate
    "min_soc_pct":           20,     # never charge below this SOC (%)
    "smart_charging":        True,   # enable smart charging logic
    "smtp": {
        "host": "", "port": 587,
        "username": "", "password": "", "from": ""
    },
    "isolarcloud": {
        "app_key":      "",
        "app_secret":   "",
        "app_id":       "3251",
        "plant_id":     "5486815",
        "device_uuid":  "4033562",
        "ps_id":        "5486815",
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
        for sub in ("isolarcloud", "smtp"):
            d = {}
            d.update(DEFAULT_CONFIG[sub])
            d.update(saved.get(sub, {}))
            merged[sub] = d
        return merged
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(str(CONFIG_FILE), "w") as f:
        json.dump(cfg, f, indent=2)

# ---------------------------------------------------------------------------
# Token helpers
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
# iSolarCloud HTTP helpers
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
        log.error("No token.json found")
        return None

    expires_at = token.get("expires_at", 0)
    now        = int(datetime.now(timezone.utc).timestamp())

    if expires_at - now < 3600:   # refresh if less than 1h remaining
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

# ---------------------------------------------------------------------------
# Live battery data from iSolarCloud
# ---------------------------------------------------------------------------

def fetch_battery_state(iso_cfg):
    """
    Fetch current battery SOC and today's energy data from iSolarCloud.
    Returns dict with:
      soc_pct         — battery state of charge (0-100)
      load_power_w    — current household consumption in watts
      charged_today_wh  — energy charged into battery today
      discharged_today_wh — energy discharged from battery today
      grid_bought_wh  — grid energy purchased today
      load_today_wh   — total household consumption today
    """
    access_token = get_valid_token(iso_cfg)
    if not access_token:
        log.warning("No token — skipping live battery fetch")
        return None

    try:
        result = isolar_post(
            "/openapi/platform/getPowerStationRealTimeData",
            {
                "ps_id_list":        [int(iso_cfg["ps_id"])],
                "point_id_list":     [
                    "83252",   # battery SOC
                    "83106",   # load power now (W)
                    "83322",   # battery charged today (Wh)
                    "83323",   # battery discharged today (Wh)
                    "83102",   # grid purchased today (Wh)
                    "83118",   # daily load consumption (Wh)
                ],
                "is_get_point_dict": "1",
            },
            iso_cfg,
            access_token,
        )

        if result.get("result_code") != "1":
            log.error("Battery state fetch failed: {}".format(result))
            return None

        device = result["result_data"]["device_point_list"][0]

        def val(key, default=None):
            v = device.get(key)
            if v is None:
                return default
            try:
                return float(v)
            except (ValueError, TypeError):
                return default

        state = {
            "soc_pct":              val("p83252", 50.0) * 100
                                    if val("p83252", 0) <= 1.0
                                    else val("p83252", 50.0),
            "load_power_w":         val("p83106", 0),
            "charged_today_wh":     val("p83322", 0),
            "discharged_today_wh":  val("p83323", 0),
            "grid_bought_wh":       val("p83102", 0),
            "load_today_wh":        val("p83118", 0),
        }
        log.info("Battery state: SOC={:.1f}%, load={:.0f}W, "
                 "charged={:.0f}Wh, bought={:.0f}Wh".format(
            state["soc_pct"], state["load_power_w"],
            state["charged_today_wh"], state["grid_bought_wh"]))
        return state

    except Exception as e:
        log.error("fetch_battery_state failed: {}".format(e))
        return None

# ---------------------------------------------------------------------------
# Smart charging decision
# ---------------------------------------------------------------------------

def should_grid_charge(cfg, current_hour, battery_state, solar_tomorrow):
    """
    Decide whether grid charging is actually needed this hour.

    Logic:
      1. Calculate energy remaining in battery right now
      2. Calculate expected household consumption until tomorrow morning
         (when solar kicks in)
      3. Calculate expected solar yield tomorrow
      4. If (battery + solar_tomorrow) >= consumption_needed -> skip charge
      5. If shortfall -> charge needed

    Returns (should_charge: bool, reason: str, details: dict)
    """
    if not cfg.get("smart_charging", True):
        return True, "Smart charging disabled — using simple mode", {}

    if battery_state is None:
        return True, "Could not fetch battery state — defaulting to charge", {}

    battery_kwh    = cfg["battery_capacity_kwh"]
    panel_kwp      = cfg["panel_capacity_kwp"]
    efficiency     = cfg["panel_efficiency"]
    min_soc        = cfg["min_soc_pct"]
    avg_daily_kwh  = cfg["avg_consumption_kwh"]

    # Current stored energy (usable above min_soc)
    soc_pct        = battery_state["soc_pct"]
    usable_kwh     = battery_kwh * max(0, (soc_pct - min_soc) / 100.0)

    # Hours until solar starts (assume solar from 07:00)
    solar_start_hour = 7
    if current_hour >= solar_start_hour:
        hours_until_solar = (24 - current_hour) + solar_start_hour
    else:
        hours_until_solar = solar_start_hour - current_hour

    # Expected consumption until solar starts
    hourly_consumption_kwh  = avg_daily_kwh / 24.0
    consumption_until_solar = hourly_consumption_kwh * hours_until_solar

    # Expected solar yield tomorrow (kWh)
    # GHI in W/m², panel area = capacity / (1000 * efficiency)
    total_ghi_tomorrow = sum(s["ghi"] for s in solar_tomorrow)  # W/m² summed over hours
    # Convert: kWh = sum(GHI_W_m2) * panel_kwp * efficiency / 1000
    solar_yield_kwh = total_ghi_tomorrow * panel_kwp * efficiency / 1000.0

    # Can we cover consumption_until_solar with battery + morning solar?
    morning_solar_kwh = solar_yield_kwh * 0.3   # roughly 30% of daily solar is morning
    covered_kwh       = usable_kwh + morning_solar_kwh
    shortfall_kwh     = max(0, consumption_until_solar - covered_kwh)

    details = {
        "soc_pct":               round(soc_pct, 1),
        "usable_kwh":            round(usable_kwh, 2),
        "hours_until_solar":     hours_until_solar,
        "consumption_until_solar_kwh": round(consumption_until_solar, 2),
        "solar_yield_tomorrow_kwh":    round(solar_yield_kwh, 2),
        "morning_solar_kwh":     round(morning_solar_kwh, 2),
        "shortfall_kwh":         round(shortfall_kwh, 2),
    }

    if shortfall_kwh <= 0:
        reason = (
            "Smart: battery {:.0f}% ({:.1f}kWh usable) + "
            "tomorrow solar ({:.1f}kWh) covers {:.1f}h consumption "
            "— skipping grid charge".format(
                soc_pct, usable_kwh, solar_yield_kwh, hours_until_solar)
        )
        return False, reason, details
    else:
        reason = (
            "Smart: shortfall {:.1f}kWh "
            "(battery {:.0f}%, solar {:.1f}kWh, need {:.1f}kWh) "
            "— grid charging".format(
                shortfall_kwh, soc_pct, solar_yield_kwh,
                consumption_until_solar)
        )
        return True, reason, details

# ---------------------------------------------------------------------------
# Battery control — forced charging time windows
# ---------------------------------------------------------------------------

def _isolar_set_params(iso_cfg, access_token, param_list, task_name):
    """Send a paramSetting call with multiple params at once."""
    result = isolar_post(
        "/openapi/platform/paramSetting",
        {
            "set_type":      0,
            "uuid":          str(iso_cfg["device_uuid"]),
            "task_name":     task_name,
            "expire_second": 300,
            "param_list":    param_list,
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
    return success, result


def set_forced_charging_windows(iso_cfg, windows, dry_run=False):
    """
    Set forced charging time windows on the inverter.

    windows: list of 0, 1 or 2 dicts:
      [{"start_h": 2, "start_m": 0, "end_h": 5, "end_m": 0, "target_soc": 90}, ...]

    Uses params:
      10065 = forced_charging enable (1=on, 0=off)
      10067/68/69/70/71 = window 1 start_h/start_m/end_h/end_m/target_soc
      10072/73/74/75/76 = window 2 start_h/start_m/end_h/end_m/target_soc
    """
    if dry_run:
        if windows:
            for i, w in enumerate(windows):
                log.info("DRY RUN — would set charging window {}: "
                         "{:02d}:{:02d}–{:02d}:{:02d} target {}%".format(
                    i+1, w["start_h"], w["start_m"],
                    w["end_h"], w["end_m"], w["target_soc"]))
        else:
            log.info("DRY RUN — would disable forced charging windows")
        return {"status": "dry_run", "windows": windows}

    if not iso_cfg.get("app_key") or not iso_cfg.get("app_secret"):
        return {"status": "error", "msg": "not configured"}

    access_token = get_valid_token(iso_cfg)
    if not access_token:
        return {"status": "error", "msg": "no valid token"}

    try:
        # Build param list
        params = []

        if not windows:
            # Disable forced charging
            params.append({"param_code": "10065", "set_value": "0"})
        else:
            # Enable forced charging
            params.append({"param_code": "10065", "set_value": "1"})

            # Window 1 (always set)
            w1 = windows[0]
            params += [
                {"param_code": "10067", "set_value": str(w1["start_h"])},
                {"param_code": "10068", "set_value": str(w1["start_m"])},
                {"param_code": "10069", "set_value": str(w1["end_h"])},
                {"param_code": "10070", "set_value": str(w1["end_m"])},
                {"param_code": "10071", "set_value": str(w1["target_soc"])},
            ]

            # Window 2 (if provided)
            if len(windows) >= 2:
                w2 = windows[1]
                params += [
                    {"param_code": "10072", "set_value": str(w2["start_h"])},
                    {"param_code": "10073", "set_value": str(w2["start_m"])},
                    {"param_code": "10074", "set_value": str(w2["end_h"])},
                    {"param_code": "10075", "set_value": str(w2["end_m"])},
                    {"param_code": "10076", "set_value": str(w2["target_soc"])},
                ]

        task_name = "BatteryAgent {}".format(
            datetime.now().strftime("%Y-%m-%d %H:%M"))
        success, result = _isolar_set_params(
            iso_cfg, access_token, params, task_name)

        if success:
            if windows:
                for i, w in enumerate(windows):
                    log.info("Charging window {} set: {:02d}:{:02d}–{:02d}:{:02d} "
                             "target {}%".format(
                        i+1, w["start_h"], w["start_m"],
                        w["end_h"], w["end_m"], w["target_soc"]))
            else:
                log.info("Forced charging disabled OK")
            return {"status": "ok", "windows": windows}
        else:
            log.error("Failed to set charging windows: {}".format(result))
            return {"status": "error", "msg": str(result)}

    except Exception as e:
        log.error("set_forced_charging_windows failed: {}".format(e))
        return {"status": "error", "msg": str(e)}


def compute_charging_windows(cfg, schedule_today, battery_state, solar_tmrw):
    """
    Given today's schedule, compute up to 2 forced charging windows.

    Groups consecutive cheap hours into windows, picks the 2 best blocks,
    calculates target SOC based on smart charging math.

    Returns list of 0-2 window dicts.
    """
    cheap_hours = sorted(
        [s["hour"] for s in schedule_today if s["mode"] == "grid_charge"])

    if not cheap_hours:
        return []

    # Group consecutive hours into blocks
    blocks = []
    block  = [cheap_hours[0]]
    for h in cheap_hours[1:]:
        if h == block[-1] + 1:
            block.append(h)
        else:
            blocks.append(block)
            block = [h]
    blocks.append(block)

    # Sort blocks by average price (cheapest first)
    def avg_price(block):
        prices = [s["price_SEK"] or 999
                  for s in schedule_today if s["hour"] in block]
        return sum(prices) / len(prices) if prices else 999

    blocks.sort(key=avg_price)

    # Calculate target SOC
    battery_kwh   = cfg["battery_capacity_kwh"]
    panel_kwp     = cfg["panel_capacity_kwp"]
    efficiency    = cfg["panel_efficiency"]
    min_soc       = cfg["min_soc_pct"]
    avg_daily_kwh = cfg["avg_consumption_kwh"]
    soc_pct       = battery_state["soc_pct"] if battery_state else 50.0

    total_ghi_tmrw  = sum(s["ghi"] for s in solar_tmrw)
    solar_yield_kwh = total_ghi_tmrw * panel_kwp * efficiency / 1000.0
    hourly_kwh      = avg_daily_kwh / 24.0
    # Assume we're setting windows for overnight — estimate hours until solar
    hours_until_solar  = 8   # conservative: assume 8h until panels kick in
    consumption_needed = hourly_kwh * hours_until_solar
    morning_solar      = solar_yield_kwh * 0.3

    shortfall_kwh = max(0, consumption_needed - morning_solar)
    current_stored = battery_kwh * max(0, (soc_pct - min_soc) / 100.0)
    charge_needed_kwh = max(0, shortfall_kwh - current_stored)

    # Target SOC = current + what we need to charge, capped at 100%
    charge_needed_pct = (charge_needed_kwh / battery_kwh) * 100
    target_soc = min(100, int(soc_pct + charge_needed_pct + 10))  # +10 buffer
    target_soc = max(target_soc, min_soc + 20)  # always at least min_soc+20

    log.info("Charging windows: shortfall={:.1f}kWh, target_soc={}%".format(
        shortfall_kwh, target_soc))

    # Build up to 2 windows from the 2 cheapest blocks
    windows = []
    for block in blocks[:2]:
        start_h = block[0]
        end_h   = block[-1] + 1   # end hour is exclusive
        if end_h >= 24:
            end_h = 23
        windows.append({
            "start_h":    start_h,
            "start_m":    0,
            "end_h":      end_h,
            "end_m":      0,
            "target_soc": target_soc,
            "is_tomorrow": start_h < 12,  # early morning hours are next calendar day
        })

    return windows

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
    date_str     = date.strftime("%Y-%m-%d")
    tomorrow_str = (date + timedelta(days=1)).strftime("%Y-%m-%d")
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
# Schedule computation
# ---------------------------------------------------------------------------

def compute_schedule_for_day(cfg, prices, solar_slots,
                              battery_state=None, solar_tomorrow=None,
                              current_hour=None):
    """
    Compute 24-slot schedule for a single day.
    If battery_state and solar_tomorrow provided, uses smart charging logic.
    """
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

    # Smart charging check — only for future cheap hours
    smart_details  = {}
    smart_skip_hrs = set()
    if battery_state is not None and solar_tomorrow is not None:
        for h in list(cheap_hours):
            if current_hour is not None and h <= current_hour:
                continue   # already past
            charge, reason, details = should_grid_charge(
                cfg, h, battery_state, solar_tomorrow)
            if not charge:
                smart_skip_hrs.add(h)
                smart_details[h] = {"skipped": True, "reason": reason,
                                    "details": details}
                log.info("Hour {}: smart skip — {}".format(h, reason))
            else:
                smart_details[h] = {"skipped": False, "reason": reason,
                                    "details": details}

    slots = []
    for hour in range(24):
        price = price_by_hour.get(hour)
        ghi   = ghi_by_hour.get(hour, 0)

        if hour in solar_hours:
            mode   = "solar"
            reason = "Solar generating ({:.0f} W/m2 >= {})".format(
                ghi, threshold)
        elif hour in cheap_hours and hour not in smart_skip_hrs:
            mode   = "grid_charge"
            reason = "Cheapest non-solar hour (rank #{})".format(
                cheap_rank.get(hour, "?"))
        elif hour in smart_skip_hrs:
            mode   = "normal"
            d      = smart_details.get(hour, {})
            reason = d.get("reason", "Smart: solar sufficient — skipping charge")
        else:
            mode   = "normal"
            reason = "Self-consumption"

        slot = {
            "hour":      hour,
            "price_SEK": round(price, 4) if price is not None else None,
            "ghi_W_m2":  ghi,
            "mode":      mode,
            "reason":    reason,
        }
        if hour in smart_details:
            slot["smart"] = smart_details[hour]
        slots.append(slot)

    return slots

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(cfg, subject, body):
    smtp = cfg.get("smtp", {})
    to   = cfg.get("notify_email", "")
    if not to or not smtp.get("host"):
        log.info("Email not configured — skipping")
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
# Write status.json
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
    tomorrow     = now + timedelta(days=1)
    today_str    = now.strftime("%Y-%m-%d")
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")

    log.info("Agent tick — {}".format(now.strftime("%Y-%m-%d %H:%M")))

    # 1. Fetch prices and solar forecast
    prices_today = fetch_spot_prices(cfg["price_zone"], now)
    prices_tmrw  = fetch_spot_prices(cfg["price_zone"], tomorrow)
    solar_48h    = fetch_solar_forecast(cfg["latitude"], cfg["longitude"], now)

    if not prices_today:
        log.error("No price data — aborting")
        return

    solar_today = [s for s in solar_48h if s.get("date") == today_str]
    solar_tmrw  = [s for s in solar_48h if s.get("date") == tomorrow_str]

    # 2. Fetch live battery state (for smart charging)
    battery_state = None
    if cfg.get("smart_charging", True) and not cfg.get("dry_run", False):
        battery_state = fetch_battery_state(cfg["isolarcloud"])

    # 3. Compute schedules
    schedule_today = compute_schedule_for_day(
        cfg, prices_today, solar_today,
        battery_state=battery_state,
        solar_tomorrow=solar_tmrw,
        current_hour=current_hour,
    )
    schedule_tmrw = compute_schedule_for_day(
        cfg, prices_tmrw, solar_tmrw) if prices_tmrw else []

    # 4. Find current slot
    slot = next((s for s in schedule_today if s["hour"] == current_hour), None)
    if not slot:
        log.error("No schedule slot for hour {}".format(current_hour))
        return

    mode  = slot["mode"]
    price = slot["price_SEK"]
    ghi   = slot["ghi_W_m2"]

    log.info("Hour {}: {} | {}".format(current_hour, mode, slot["reason"]))

    # 5. Compute and set forced charging windows (once per run)
    dry_run = cfg.get("dry_run", False)
    windows = compute_charging_windows(
        cfg, schedule_today, battery_state, solar_tmrw)
    result  = set_forced_charging_windows(
        cfg["isolarcloud"], windows, dry_run=dry_run)

    # 6. Write status.json
    soc_pct = battery_state["soc_pct"] if battery_state else None
    status = {
        "last_run":          now.isoformat(),
        "current_hour":      current_hour,
        "today":             today_str,
        "tomorrow":          tomorrow_str,
        "mode":              mode,
        "windows":           windows,
        "reason":            slot["reason"],
        "price_SEK":         price,
        "price_ore":         round(price * 100, 1) if price else None,
        "ghi_W_m2":          ghi,
        "battery_soc_pct":   soc_pct,
        "battery_state":     battery_state,
        "result":            result,
        "schedule":          schedule_today,
        "schedule_tmrw":     schedule_tmrw,
        "dry_run":           dry_run,
        "smart_charging":    cfg.get("smart_charging", True),
    }
    write_status(status)

    # 7. Email notification
    if result.get("status") in ("ok", "dry_run"):
        soc_str = "{:.1f}%".format(soc_pct) if soc_pct else "unknown"
        subject = "Battery Agent: {:02d}:00 — {} | SOC {}".format(
            current_hour, mode, soc_str)
        body = (
            "Solar Battery Agent Report\n"
            "==========================\n"
            "Time:        {:02d}:00\n"
            "Mode:        {}\n"
            "Reason:      {}\n"
            "Battery SOC: {}\n"
            "Solar GHI:   {} W/m2\n"
            "Status:      {}\n"
        ).format(current_hour, mode, slot["reason"],
                 soc_str, ghi, result.get("status"))

        if windows:
            body += "\nForced charging windows set:\n"
            for i, w in enumerate(windows):
                body += "  Window {}: {:02d}:00-{:02d}:00 target {}%\n".format(
                    i+1, w["start_h"], w["end_h"], w["target_soc"])
        else:
            body += "\nNo grid charging needed tonight.\n"

        skipped = [s for s in schedule_today if s.get("smart", {}).get("skipped")]
        if skipped:
            body += "\nSmart: skipped {} cheap hour(s) — solar sufficient\n".format(
                len(skipped))

        send_email(cfg, subject, body)
    else:
        send_email(
            cfg,
            "Battery Agent ERROR at {:02d}:00".format(current_hour),
            "Error: {}\nCheck: ~/solar-agent/logs/agent.log".format(
                result.get("msg"))
        )

    log.info("Done.")


if __name__ == "__main__":
    main()
