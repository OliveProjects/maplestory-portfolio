"""Inställningarna ur installningar.json, för alla skript i mappen.

Kopiera installningar.exempel.json till installningar.json och fyll i den.
Fälten förklaras i LASMIG.md.
"""
import json
import os
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
_FIL = os.path.join(HERE, "installningar.json")
if not os.path.exists(_FIL):
    raise SystemExit("installningar.json is missing: run python website_creator.py, or copy "
                     "installningar.exempel.json to installningar.json in the same folder and fill it in.")
_I = json.load(open(_FIL, encoding="utf-8"))

KARAKTARER = _I["karaktarer"]
BUTIK = _I.get("butik") or (KARAKTARER[0] if KARAKTARER else "")
REF = _I.get("ref", "").strip()
SAJTNAMN = _I.get("sajtnamn", "")
SAJT_URL = _I["sajt_url"].rstrip("/") + "/"
TIDSZON = ZoneInfo(_I.get("tidszon", "Europe/Stockholm"))
TIDSZON_NAMN = _I.get("tidszon", "Europe/Stockholm")
WEB = _I.get("webbrepo", "")
UNDERMAPP = _I.get("undermapp", "maplestory")
REMOTES = _I.get("git_remotes", ["origin"])
WZ_DATA = _I.get("wz_data", r"C:\Nexon\Library\maplestorycw\appdata\Data")
USER_AGENT = _I.get("user_agent") or SAJT_URL


def sida(web=None):
    """Sidans mapp i webbrepot (eller i sajten på servern)."""
    return os.path.join(web or WEB, UNDERMAPP)


def data(web=None):
    return os.path.join(sida(web), "data")
