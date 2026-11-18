"""The settings from settings.json, for every script in this folder.

website_creator.py writes settings.json. To write it by hand, copy
settings.example.json to settings.json and fill it in; the fields are
explained in the README. Settings files from older versions, with Swedish
keys (karaktarer, sajt_url ...), still work.
"""
import json
import os
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
_FILE = os.path.join(HERE, "settings.json")
if not os.path.exists(_FILE):
    raise SystemExit("settings.json is missing: run python website_creator.py, or copy "
                     "settings.example.json to settings.json in the same folder and fill it in.")
_S = json.load(open(_FILE, encoding="utf-8"))

# English key -> the Swedish key older versions wrote.
OLD_KEYS = {"characters": "karaktarer", "shop_character": "butik", "site_name": "sajtnamn",
            "site_url": "sajt_url", "time_zone": "tidszon", "site_repo": "webbrepo", "subfolder": "undermapp"}


def get(key, default=None):
    """A setting by its English key, or by the Swedish key an older version wrote."""
    if key in _S:
        return _S[key]
    return _S.get(OLD_KEYS.get(key, key), default)


CHARACTERS = get("characters", [])
SHOP_CHARACTER = get("shop_character") or (CHARACTERS[0] if CHARACTERS else "")
REF = (get("ref") or "").strip()
SITE_NAME = get("site_name", "")
SITE_URL = get("site_url").rstrip("/") + "/"
TIME_ZONE_NAME = get("time_zone", "Europe/Stockholm")
TIME_ZONE = ZoneInfo(TIME_ZONE_NAME)
WEB = get("site_repo", "")
SUBFOLDER = get("subfolder", "maplestory")
REMOTES = get("git_remotes", ["origin"])
WZ_DATA = get("wz_data", r"C:\Nexon\Library\maplestorycw\appdata\Data")
USER_AGENT = get("user_agent") or SITE_URL


def page_dir(web=None):
    """The page's folder in the site repo (or in the site on a server)."""
    return os.path.join(web or WEB, SUBFOLDER)


def data_dir(web=None):
    return os.path.join(page_dir(web), "data")
