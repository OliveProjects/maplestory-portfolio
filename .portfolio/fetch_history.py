"""Hämtar timhistoriken för varje föremål som någon gång legat i Deco-fliken.

  python fetch_history.py [--web <utcheckning>] [--sen-publicera]

  python fetch_history.py --rotera --web <utcheckning>   (varje timme, run_hourly.sh)

--sen-publicera kör market_prices.py --bakat när allt är hämtat, som bygger
om prices.json och pushar. Första gången: se LASMIG.md.

En fil per föremål i historik/<marknadens id>.json. Det som redan är hämtat
hoppas över, så skriptet kan avbrytas och startas om. Nyckeln ligger i
api_key.txt bredvid, aldrig i webbrepot (API-villkoren: aldrig i sidans kod).

Nyckeln får 6 anrop i timmen. Skriptet läser x-ratelimit-remaining och
x-ratelimit-reset i varje svar och väntar till nästa timme när det är slut.
Historiken går en vecka bakåt.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import settings as inst

HERE = os.path.dirname(os.path.abspath(__file__))
UT = os.path.join(HERE, "history")
PRICES = os.path.join(inst.data(), "prices.json")
# Vår klients id -> marknadens, där de skiljer sig.
TILL_MARKNAD = {"1112100": "1112222"}  # White Label Ring


def nyckel():
    return open(os.path.join(HERE, "api_key.txt"), encoding="utf-8").read().strip()


def alla_id(prices=PRICES, bara_nu=False):
    """Marknadens id för allt som någon gång ägts, eller med bara_nu bara
    det som ägs nu (sista punktens antal)."""
    upp = json.load(open(prices, encoding="utf-8"))["updates"]
    ids = []
    for u in upp[-1:] if bara_nu else upp:
        for i in (list(u.get("qty", {})) if bara_nu else list(u["prices"]) + list(u.get("qty", {}))):
            m = TILL_MARKNAD.get(i, i)
            if m not in ids:
                ids.append(m)
    return ids


# Anrop per timme för rotationen; den sjätte lämnas åt synken och liknande.
PER_TIMME = 5


def rotera(prices):
    """Serverns timjobb: hämtar historiken för de PER_TIMME föremål som hämtades
    längst sedan, dygnet runt. Ett varv över 43 föremål tar ungefär nio
    timmar, så inget föremåls pris är äldre än så. Spelet går dygnet runt,
    och det gör rotationen också. Karaktärernas butiker hämtas en gång per
    dygn; en karaktär som svarat
    404 (aldrig setts med butik) bara en gång i veckan."""
    def alder(fil):
        return os.path.getmtime(fil) if os.path.exists(fil) else 0
    # Först när priserna bygger på marknadens historik (efter --bakat);
    # innan dess delar rotationen nyckelns anrop med datorns hämtning.
    if json.load(open(prices, encoding="utf-8")).get("source") != "maplestory.market":
        return
    nu = time.time()
    ko = [("items/%s/history" % i, os.path.join(UT, i + ".json")) for i in alla_id(prices, bara_nu=True)]
    for s, f in EXTRA:
        utan_butik = os.path.exists(f) and '"error": 404' in open(f, encoding="utf-8").read(200)
        if nu - alder(f) > (7 * 86400 if utan_butik else 20 * 3600):
            ko.append((s, f))
    ko.sort(key=lambda sf: alder(sf[1]))
    for sokvag, fil in ko[:PER_TIMME]:
        data, h = hamta(sokvag)
        if data is None:
            break  # 429: timmens anrop är slut
        spara(fil, data)
        print("%s  %s" % (time.strftime("%H:%M"), sokvag), flush=True)
        if int(h.get("x-ratelimit-remaining", 1)) <= 0:
            break


# Andra anrop som ska med i samma kö: (sökväg i API:t, fil här).
KARAKTARER = inst.KARAKTARER
# Senaste svaret per karaktär; tidigare dagars svar ligger kvar i shops.json.
EXTRA = [("owners/" + k, os.path.join(HERE, "owners", "%s.json" % k)) for k in KARAKTARER]


def spara(fil, data):
    """Skriver ett svar. Historiken slås ihop med det som redan finns, timme
    för timme (det nya svaret vinner), eftersom API:t bara ger en vecka
    bakåt: att skriva över hade kastat allt äldre. ~2 MB i veckan för 43
    föremål."""
    os.makedirs(os.path.dirname(fil), exist_ok=True)
    if os.path.dirname(fil) == UT and os.path.exists(fil):
        try:
            rader = {r["hour"]: r for r in json.load(open(fil, encoding="utf-8"))}
            rader.update({r["hour"]: r for r in json.loads(data)})
            data = json.dumps([rader[k] for k in sorted(rader)], separators=(",", ":")).encode()
        except (ValueError, KeyError, TypeError):
            pass  # oväntat format: spara svaret som det är, hellre än att stanna
    with open(fil, "wb") as f:
        f.write(data)


def hamta(sokvag):
    req = urllib.request.Request(
        "https://maplestory.market/api/" + sokvag,
        headers={"Authorization": "Bearer " + nyckel(), "User-Agent": inst.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as svar:
            return svar.read(), svar.headers
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, e.headers
        if e.code == 404 and sokvag.startswith("items/"):
            return b"[]", e.headers  # marknaden har aldrig sett föremålet
        if sokvag.startswith("owners/"):
            # En karaktär utan butik ska inte stoppa prishämtningen.
            return json.dumps({"error": e.code, "body": e.read()[:500].decode("utf-8", "replace")}).encode(), e.headers
        sluta(sokvag, e)


def sluta(iid, e):
    print("FEL %s: %s %s" % (iid, e.code, e.read()[:200]), flush=True)
    sys.exit(1)


def vanta_till(reset):
    s = max(0, int(reset) - time.time()) + 30
    print("  väntar %d min" % (s // 60), flush=True)
    time.sleep(s)


def main(args):
    os.makedirs(UT, exist_ok=True)
    if "--rotera" in args:
        web = args[args.index("--web") + 1] if "--web" in args else inst.WEB
        rotera(os.path.join(inst.data(web), "prices.json"))
        return
    web = args[args.index("--web") + 1] if "--web" in args else inst.WEB
    prices = os.path.join(inst.data(web), "prices.json")
    kvar = [(s, f) for s, f in EXTRA if not os.path.exists(f)] + [
        ("items/%s/history" % i, os.path.join(UT, i + ".json")) for i in alla_id(prices)
        if not os.path.exists(os.path.join(UT, i + ".json"))]
    print("%d anrop kvar" % len(kvar), flush=True)
    while kvar:
        sokvag, fil = kvar[0]
        data, h = hamta(sokvag)
        if data is None:  # 429: vänta och försök igen
            vanta_till(h.get("x-ratelimit-reset") or time.time() + int(h.get("Retry-After", 600)))
            continue
        spara(fil, data)
        kvar.pop(0)
        print("%s  %s  %d kvar" % (time.strftime("%H:%M"), sokvag, len(kvar)), flush=True)
        if kvar and int(h.get("x-ratelimit-remaining", 1)) <= 0:
            vanta_till(h["x-ratelimit-reset"])
    print("klar", flush=True)
    if "--sen-publicera" in args:
        import market_prices
        market_prices.main(["--bakat", "--web", web])


if __name__ == "__main__":
    main(sys.argv[1:])
