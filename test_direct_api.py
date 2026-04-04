"""
test_direct_api.py
==================
Tests direct HTTP iSolarCloud calls matching pysolarcloud exactly.

Key findings from pysolarcloud source:
  - x-access-key header = APP_SECRET (not app_key!)
  - appkey in body     = APP_KEY
  - Token endpoint     = /openapi/apiManage/token
  - Refresh endpoint   = /openapi/apiManage/refreshToken
  - Control endpoint   = /openapi/platform/paramSetting

Run from the folder containing token.json:
  python3 test_direct_api.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Credentials
# Note the naming: APP_KEY goes in the body as 'appkey'
#                  APP_SECRET goes in the header as 'x-access-key'
# ---------------------------------------------------------------------------
APP_KEY     = "060A4E55C654426D4CBCD58B1B1DD5FA"
APP_SECRET  = "zx3edv413yy4etuzjgf32rnnmumzq7xr"
APP_ID      = "3251"
DEVICE_UUID = "4033562"
BASE_URL    = "https://gateway.isolarcloud.eu"
TOKEN_FILE  = Path("token.json")
# ---------------------------------------------------------------------------


def load_token():
    if TOKEN_FILE.exists():
        with open(str(TOKEN_FILE)) as f:
            return json.load(f)
    return None


def save_token(token):
    with open(str(TOKEN_FILE), "w") as f:
        json.dump(token, f, indent=2, default=str)


def make_headers(access_token=None):
    """
    x-access-key = APP_SECRET  (pysolarcloud: self.access_key = client_secret)
    Authorization = Bearer {token}
    """
    h = {
        "Content-Type": "application/json",
        "x-access-key":  APP_SECRET,
    }
    if access_token:
        h["Authorization"] = "Bearer {}".format(access_token)
    return h


def make_body(data, access_token=None):
    """
    appkey in body = APP_KEY  (pysolarcloud: self.appkey = client_id)
    """
    body = dict(data)
    body["appkey"] = APP_KEY
    body["lang"]   = "_en_US"
    return body


def get_valid_token():
    token = load_token()
    if not token:
        print("No token.json found.")
        sys.exit(1)

    expires_at = token.get("expires_at", 0)
    now        = int(datetime.now(timezone.utc).timestamp())

    if expires_at - now < 60:
        print("Token expired — refreshing...")
        resp = requests.post(
            "{}/openapi/apiManage/refreshToken".format(BASE_URL),
            headers=make_headers(),
            json={"appkey": APP_KEY, "refresh_token": token["refresh_token"]},
            timeout=15,
        )
        print("Refresh status:", resp.status_code)
        data = resp.json()
        print(json.dumps(data, indent=2))
        if "access_token" in data:
            token.update(data)
            token["expires_at"] = now + int(data.get("expires_in", 172799)) - 20
            save_token(token)
        else:
            print("Refresh failed — re-authorize.")
            sys.exit(1)

    return token["access_token"]


def api_post(path, data, access_token):
    resp = requests.post(
        "{}{}".format(BASE_URL, path),
        headers=make_headers(access_token),
        json=make_body(data),
        timeout=15,
    )
    return resp


def separator(title):
    print("\n" + "=" * 60)
    print("  {}".format(title))
    print("=" * 60)


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------
access_token = get_valid_token()
print("Token OK: {}...".format(access_token[:20]))

separator("STEP 1: Read charge_discharge_command")
resp = api_post(
    "/openapi/platform/paramSetting",
    {
        "set_type":      2,
        "uuid":          DEVICE_UUID,
        "task_name":     "Read {}".format(datetime.now().strftime("%H:%M")),
        "expire_second": 120,
        "param_list":    [{"param_code": "10004", "set_value": ""}],
    },
    access_token,
)
print("Status:", resp.status_code)
print(json.dumps(resp.json(), indent=2))

separator("STEP 2: Set to Charge (value=170)")
resp = api_post(
    "/openapi/platform/paramSetting",
    {
        "set_type":      0,
        "uuid":          DEVICE_UUID,
        "task_name":     "Charge {}".format(datetime.now().strftime("%H:%M")),
        "expire_second": 120,
        "param_list":    [{"param_code": "10004", "set_value": "170"}],
    },
    access_token,
)
print("Status:", resp.status_code)
print(json.dumps(resp.json(), indent=2))

separator("STEP 3: Set back to Stop (value=204)")
resp = api_post(
    "/openapi/platform/paramSetting",
    {
        "set_type":      0,
        "uuid":          DEVICE_UUID,
        "task_name":     "Stop {}".format(datetime.now().strftime("%H:%M")),
        "expire_second": 120,
        "param_list":    [{"param_code": "10004", "set_value": "204"}],
    },
    access_token,
)
print("Status:", resp.status_code)
print(json.dumps(resp.json(), indent=2))

print("\nDone. Battery should be back to Stop.")
