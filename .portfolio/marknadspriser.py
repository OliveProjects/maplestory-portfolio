"""Priser ur maplestory.market till sidan.

  python marknadspriser.py --bakat [--ut <mapp>] [--ingen-push]
      Bygger om hela prices.json ur historik/ (hamta_historik.py), en punkt
      per dag från en vecka före första inventeringen till i dag (så långt
      bakåt som API:ts historik går). Händelsernas marknadspris i
      activity.json räknas om på samma sätt.

  python marknadspriser.py --live --live-ut <fil> [--web <utcheckning>]
      Livepriserna ur den historik som finns, varje timme (kor.sh på servern).

  python marknadspriser.py [--web <utcheckning>] [--remotes origin] [--ingen-push]
      Första körningen efter midnatt sparar gårdagens sista livevärde som
      dagens punkt i prices.json. Ingen stängning: spelet går dygnet runt.

Priset är den styckviktade medianen av timmarnas säljmedianer, i det
kortaste fönster (24h, 72h, 7d) som har tillräckligt med försäljningar; se
markera() och FONSTER_REGLER. Bakåt räknas samma mått ur timhistoriken, med
dygnets slut (kl. 24 svensk tid) som mätpunkt.

Dagarna före första inventeringen räknas med den inventeringens antal.
Sådana punkter får "assumed_qty": true, och sidan ritar dem prickade.

Git: hämtar (pull --rebase) innan något skrivs, eftersom både servern och
datorn med spelet committar till samma repo.

API-villkoren (maplestory.market/docs/api/terms): krediten och länkarna står
på sidan, nyckeln stannar i nyckel.txt, och svaren sparas i stället för att
hämtas om. Bara procent och totaler publiceras ur historiken, aldrig gamla
priser per föremål i bulk.
"""
import datetime
import json
import os
import statistics
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import installningar as inst  # noqa: E402
from hamta_historik import KARAKTARER, TILL_MARKNAD, nyckel  # noqa: E402

HISTORIK = os.path.join(HERE, "historik")
SVERIGE = inst.TIDSZON  # sidans tidszon; dygnet slutar vid midnatt där
# En dag får inte flytta priserna mer än så här utan att någon tittar. Var
# 0,25; en dag steg kontot ~29 % på riktigt (en Founder-låda slutade säljas
# och två föremål dubblades).
MAX_HOPP = 0.40


def mid(iid):
    return TILL_MARKNAD.get(iid, iid)


def skriv_json(fil, data):
    os.makedirs(os.path.dirname(fil), exist_ok=True)
    with open(fil, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


def total(u):
    return sum(u["prices"].get(i, 0) * q for i, q in u["qty"].items())


def kontrollera(updates):
    """Stoppar innan något pushas om totalen hoppar orimligt mellan två
    dagar. Ett föremål utan pris (som aldrig setts säljas) är bara en
    varning; sidan visar det som "no recorded price"."""
    fel = []
    for u in updates:
        saknas = [i for i in u["qty"] if i not in u["prices"]]
        if saknas:
            print("varning: %s saknar pris för %s" % (u["date"], ", ".join(saknas)))
    for a, b in zip(updates, updates[1:]):
        # Prisrörelsen på b:s antal: köp och försäljningar är inget hopp.
        gem = [i for i in b["qty"] if i in a["prices"] and i in b["prices"]]
        fore = sum(a["prices"][i] * b["qty"][i] for i in gem)
        efter = sum(b["prices"][i] * b["qty"][i] for i in gem)
        if fore and abs(efter / fore - 1) > MAX_HOPP:
            fel.append("priserna hoppar %s -> %s: %d -> %d på samma antal" % (a["date"], b["date"], fore, efter))
    if fel:
        sys.exit("STOPP, inget skrivet:\n  " + "\n  ".join(fel))


def matpunkt(dag):
    """Dygnets slut (kl. 24 i sidans tidszon) som unix-sekunder: dagens
    punkt i historiken är dygnets sista värde. Ingen stängning; spelet och
    sidan går dygnet runt."""
    d = dag + datetime.timedelta(days=1)
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=SVERIGE).timestamp())


# ── Värderingen: hög volym = kortare fönster ──
# Fönster, kortast först: (namn, timmar, minst sålda st, minst timmar med försäljning).
# Kravet på spridda timmar hindrar att en enda köpare sätter priset.
# 24h-kravet sänkt från 20 st/6 timmar till 10/5: hellre ett dagspris som rör
# sig. En dag då marknaden registrerade en sjättedel av dagen, med tomma
# timmar, föll nio sorter som brukar sälja 30–190 st om dagen till 72h; med
# 10/5 klarade 41 av 43 den dagen. Takregeln (25 % per
# timme) och kravet på fem timmar hindrar fortfarande en enda köpare.
FONSTER_REGLER = [("24h", 24, 10, 5), ("72h", 72, 8, 4), ("7d", 168, 3, 2)]
ORDNING = {"24h": 0, "72h": 1, "7d": 2, "stale": 3}
TAK = 0.25          # ingen enskild timme väger mer än så i medianen
LIKVID_ANDEL = 0.2  # så stor del av dagens försäljningar en säljare kan ta utan att trycka priset


def viktad_median(par):
    """par = [(värde, vikt)]; värdet där halva vikten passerats."""
    par = sorted(par)
    halv = sum(v for _, v in par) / 2
    acc = 0
    for varde, vikt in par:
        acc += vikt
        if acc >= halv:
            return varde
    return None


def timmar_med_salg(timmar, t, h):
    return [r for r in timmar if t - h * 3600 <= r["hour"] < t and r.get("sold")]


def kvalificerar(timmar, t):
    """Kortaste fönster som uppfyller sitt krav, annars "stale"."""
    for namn, h, min_st, min_tim in FONSTER_REGLER:
        rader = timmar_med_salg(timmar, t, h)
        if sum(r["sold"]["units"] for r in rader) >= min_st and len(rader) >= min_tim:
            return namn
    return "stale"


def fonsterpris(timmar, t, w):
    """Priset vid t i fönstret w ("24h", "72h", "7d"), eller None."""
    h = dict((n, hh) for n, hh, _, _ in FONSTER_REGLER)[w]
    # Udda timmar jämförs med fönstrets typiska timme (oviktad median av
    # timmarnas medianer), inte med veckans: ett föremål som på ett dygn gick
    # från 220k till 450k hade annars fått de nya affärerna bortfiltrerade.
    # En skräptimme (103 st à 481 miljoner) väger en timme, inte 103 st.
    fonster = timmar_med_salg(timmar, t, h)
    if not fonster:
        return None
    vm = statistics.median(r["sold"]["median_unit_price"] for r in fonster)
    rader = [r for r in fonster if 0.5 * vm <= r["sold"]["median_unit_price"] <= 2 * vm]
    total_st = sum(r["sold"]["units"] for r in rader)
    par = [(r["sold"]["median_unit_price"], min(r["sold"]["units"], TAK * total_st)) for r in rader]
    return viktad_median(par)


def markera(timmar, t, forra=None):
    """Dagens pris för ett föremål vid tiden t.

    Värdet är den styckviktade medianen av timmarnas säljmedianer i det
    valda fönstret: en udda affär flyttar ett snitt men inte en median.
    Timmar utanför 0,5–2 × fönstrets typiska timme räknas inte, och ingen timme
    väger mer än TAK. Fönstret byts till ett kortare först när det kortare
    har gällt två dagar i rad (forra = gårdagens {"w", "q", "p"}); annars
    skapar bytet i sig falska dagsrörelser. Har inget sålts på en vecka:
    det lägsta av gårdagens pris och 85 % av utropspriset, märkt "stale".

    Returnerar {"p": pris, "w": fönster som användes, "q": kortaste som
    kvalificerade i dag} eller None."""
    q = kvalificerar(timmar, t)
    w = q
    if forra and ORDNING[q] < ORDNING.get(forra.get("w"), 3):
        # Kortare än i går: bara så kort som både i dag och i går klarar.
        namn = {v: k for k, v in ORDNING.items()}
        w = namn[min(max(ORDNING[q], ORDNING.get(forra.get("q"), 3)), ORDNING[forra["w"]])]
        if w == "stale":
            w = q  # försäljningar igen efter en tom vecka: ta dem direkt
    if w != "stale":
        p = fonsterpris(timmar, t, w)
        if p:
            return {"p": round(p), "w": w, "q": q}
    # Inget sålt på en vecka: utropspriserna är ett tak, inte ett värde.
    utrop = sorted(r["seen"]["median_unit_price"] for r in timmar
                   if t - 24 * 3600 <= r["hour"] < t and r.get("seen"))
    kandidater = [x for x in ((forra or {}).get("p"), utrop[len(utrop) // 2] * 0.85 if utrop else None) if x]
    return {"p": round(min(kandidater)), "w": "stale", "q": q} if kandidater else None


def likviditet(timmar, t, antal):
    """Sålda per dag (veckans snitt) och dagar att sälja innehavet om man
    tar en femtedel av flödet."""
    # Skräptimmar bort: en timme med 103 st à 481 miljoner hade annars
    # dubblat "sold a day". Brett (0,2–5 × veckans
    # typiska timme), så att en verklig prisrörelse under veckan räknas.
    vecka = timmar_med_salg(timmar, t, 168)
    vm = statistics.median(r["sold"]["median_unit_price"] for r in vecka) if vecka else 0
    st = sum(r["sold"]["units"] for r in vecka if 0.2 * vm <= r["sold"]["median_unit_price"] <= 5 * vm)
    dagar = min(7, max(1, (t - timmar[0]["hour"]) / 86400)) if timmar else 7
    per_dag = st / dagar
    return {"per_day": round(per_dag, 1),
            "days_to_sell": round(antal / (LIKVID_ANDEL * per_dag), 1) if per_dag else None}


METOD = ("unit-weighted median of hourly sold medians; window 24h/72h/7d by volume "
         "(10 units in 5 hours / 8 in 4 / 3 in 2), shorter window only after 2 days; "
         "no sales in 7 days: min(previous, 0.85 x asking median)")


def dagspunkt(historik, qty, t, forra):
    """En dags punkt: pris, använt fönster ("basis") och kortaste fönster
    som kvalificerade ("q", för tvådagarsregeln). forra = gårdagens punkt."""
    priser, basis, kval = {}, {}, {}
    for i in qty:
        g = forra and {"w": forra.get("basis", {}).get(i), "q": forra.get("q", {}).get(i),
                       "p": forra["prices"].get(i)}
        # Slutar historiken före t gäller priset där den slutar (som live och
        # timmarna), inte ett fönster med tomma timmar på slutet.
        h = historik[i]
        m = markera(h, min(t, h[-1]["hour"] + 3600) if h else t, g if g and g["w"] else None)
        if m:
            priser[i], basis[i], kval[i] = m["p"], m["w"], m["q"]
    return {"prices": priser, "qty": qty, "basis": basis, "q": kval}


def skriv_marknad(ut, post, likv, prisrader):
    """market.json: likviditet och utropspremie per föremål, för korten (en
    gång per dygn; live.json har likviditeten timme för timme). likv = {id:
    {"per_day", "days_to_sell"}}. Utropspremie = (p25 av utropspriserna −
    vårt pris) / vårt pris; negativ betyder att säljarna redan begär under
    senaste avsluten."""
    p25 = {str(r["item_id"]): r["seen"]["p25_unit_price"] for r in prisrader if r.get("seen")}
    items = {}
    for i, q in post["qty"].items():
        if i not in post["prices"]:
            continue
        rad = {k: likv.get(i, {}).get(k) for k in ("per_day", "days_to_sell")}
        rad["basis"] = post["basis"].get(i)
        if p25.get(mid(i)):
            rad["ask_premium"] = round((p25[mid(i)] / post["prices"][i] - 1) * 100, 1)
        items[i] = rad
    skriv_json(os.path.join(ut, "market.json"), {"date": post["date"], "items": items})


def las_historik(iid):
    fil = os.path.join(HISTORIK, mid(iid) + ".json")
    if not os.path.exists(fil):
        sys.exit("saknar historik för %s (%s), kör hamta_historik.py klart först" % (iid, mid(iid)))
    return json.load(open(fil, encoding="utf-8"))


def bakat(data_dir, ut):
    gammal = json.load(open(os.path.join(data_dir, "prices.json"), encoding="utf-8"))["updates"]
    # Antalen per datum: varje inventering gäller fram till nästa.
    antal_per = [(u["date"], u["qty"]) for u in gammal if u.get("qty") and not u.get("assumed_qty")]
    INVENTERING, forsta_antal = antal_per[0]
    # Så långt bakåt som API:ts historik går (en vecka).
    FORSTA_DAG = datetime.date.fromisoformat(INVENTERING) - datetime.timedelta(days=6)
    historik = {}
    for _, q in antal_per:
        for i in q:
            if i not in historik:
                historik[i] = las_historik(i)

    idag = datetime.date.today()
    nu = datetime.datetime.now(SVERIGE).timestamp()
    updates = []
    dag = FORSTA_DAG
    while dag <= idag and matpunkt(dag) <= nu:  # dagens punkt kommer efter midnatt
        d = dag.isoformat()
        qty = forsta_antal
        for dd, q in antal_per:
            if dd <= d:
                qty = q
        post = dagspunkt(historik, qty, matpunkt(dag), updates[-1] if updates else None)
        post["date"] = d
        if d < INVENTERING:
            post["assumed_qty"] = True
        updates.append(post)
        dag += datetime.timedelta(days=1)
    kontrollera(updates)
    t = matpunkt(dag - datetime.timedelta(days=1))
    try:
        rader = api("prices")  # utropspriserna, för utropspremien
    except OSError as e:
        print("utropspriserna gick inte att hämta: %s" % e)
        rader = []
    skriv_marknad(ut, updates[-1], {i: likviditet(historik[i], t, q) for i, q in updates[-1]["qty"].items()},
                  rader)
    # En inventering i dag (dygnet är inte slut): som synka.py, en post med
    # gårdagens priser och de nya antalen. Natten därpå ersätts den av en
    # riktig dagspunkt.
    senare = [u for u in gammal if u["date"] >= dag.isoformat() and u.get("qty")]
    if senare:
        q = senare[-1]["qty"]
        updates.append({"date": senare[-1]["date"], "inventory": True, "qty": q,
                        "prices": {i: p for i, p in updates[-1]["prices"].items() if i in q}})
    skriv_json(os.path.join(ut, "prices.json"), {"source": "maplestory.market", "method": METOD, "updates": updates})

    # Händelsernas "price" är marknadspriset den dagen: det som handeln och
    # uppdelningen under kontovärdet räknar mot.
    fil = os.path.join(data_dir, "activity.json")
    akt = json.load(open(fil, encoding="utf-8")) if os.path.exists(fil) else {"events": []}
    for e in akt["events"]:
        h = historik.get(e["id"]) or las_historik(e["id"])
        m = markera(h, matpunkt(datetime.date.fromisoformat(e["date"])))
        if m:
            e["price"] = m["p"]
    skriv_json(os.path.join(ut, "activity.json"), akt)

    for u in updates:
        print(u["date"], "%10s" % format(total(u), ","), "antagna antal" if u.get("assumed_qty") else "")
    # Timmarna från första dagen, i lagret (timmar.json); servern tar över därifrån.
    if ut == data_dir:
        p = timmar(data_dir, os.path.join(ut, "hourly.json"),
                   fran=matpunkt(FORSTA_DAG - datetime.timedelta(days=1)) + 3600)
        if not p:
            return
        print("%d timmar till %s" % (len(p), datetime.datetime.fromtimestamp(p[-1][0], SVERIGE).strftime("%d %b %H:%M")))


def api(sokvag):
    req = urllib.request.Request("https://maplestory.market/api/" + sokvag,
                                 headers={"Authorization": "Bearer " + nyckel(),
                                          "User-Agent": inst.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as svar:
        return json.load(svar)


def butiker(data_dir, ut, svar):
    """shops.json: allt karaktärernas butiker har sålt. /api/owners visar bara
    senaste butikerna, så varje butik (player_id) sparas här och summan
    växer i stället för att tappa gamla butiker. svar = owners-svaren."""
    fil = os.path.join(data_dir, "shops.json")
    data = json.load(open(fil, encoding="utf-8")) if os.path.exists(fil) else {"openings": {}}
    for s in svar:
        for o in s.get("openings", []):
            data["openings"][str(o["player_id"])] = {
                "owner": s["owner_name"], "name": o["name"], "opened_at": o["opened_at"],
                "last_seen": o["last_seen"], "sales": o["sales"]["sales"],
                "units": o["sales"]["units"], "mesos": round(o["sales"]["mesos"])}
    data["updated"] = datetime.datetime.now(SVERIGE).date().isoformat()
    data["source"] = "maplestory.market"
    skriv_json(os.path.join(ut, "shops.json"), data)


def idag(data_dir, ut, live_fil):
    """Gårdagens punkt i historiken: dygnets sista livevärde (live.json från
    körningen strax före midnatt). Körs varje timme; gör något bara första
    gången efter midnatt, när gårdagen saknas. En commit per dygn."""
    nu = datetime.datetime.now(SVERIGE)
    data = json.load(open(os.path.join(data_dir, "prices.json"), encoding="utf-8"))
    upp = data["updates"]
    d = (nu.date() - datetime.timedelta(days=1)).isoformat()
    # En post med inventory: true (synka.py: gårdagens priser, nya antal) är
    # ingen dagspunkt; gäller den gårdagen ersätts den, med sina antal.
    synk = upp[-1] if upp[-1].get("inventory") and upp[-1]["date"] == d else None
    if upp[-1]["date"] >= d and not synk:
        return False
    if data.get("source") != "maplestory.market":
        print("prices.json är inte byggd ur marknadens historik än (hamta_historik.py "
              "--sen-publicera, se LASMIG.md); ingen dagspunkt")
        return False
    if not os.path.exists(live_fil):
        sys.exit("live.json saknas")
    lv = json.load(open(live_fil, encoding="utf-8"))
    if nu.timestamp() - lv["updated"] > 3 * 3600:
        sys.exit("live.json är över tre timmar gammal")
    if synk:
        upp.pop()
    sist = upp[-1]
    post = {"date": d, "prices": dict(sist["prices"]), "qty": (synk or sist)["qty"],
            "basis": dict(sist.get("basis", {})), "q": dict(sist.get("q", {}))}
    for i, x in lv["items"].items():
        if i in post["qty"]:
            post["prices"][i], post["basis"][i], post["q"][i] = x["p"], x["w"], x["q"]
    upp.append(post)
    kontrollera(upp[-2:])
    skriv_json(os.path.join(ut, "prices.json"), data)
    # hourly.json skrivs av --live varje timme, inte här: med egen webbserver
    # ligger den utanför git, annars återställer varje push den (LASMIG.md).
    print(d, format(total(post), ","))
    # Utropspriserna (p25) för utropspremien: ett anrop. Är timmens anrop
    # slut publiceras priserna ändå, utan utropspremie.
    try:
        rader = api("prices")
        os.makedirs(os.path.join(HERE, "dagssvar"), exist_ok=True)  # inte "priser/": skuggar priser.py
        with open(os.path.join(HERE, "dagssvar", "prices_%s.json" % d), "w", encoding="utf-8") as f:
            json.dump(rader, f)
    except urllib.error.HTTPError as e:
        print("utropspriserna gick inte att hämta: %s" % e.code)
        rader = []
    skriv_marknad(ut, post, {i: x for i, x in lv["items"].items()}, rader)
    # Butikerna: svaren som omgången hämtade (404 = aldrig setts med butik).
    svar = []
    for k in KARAKTARER:
        fil = os.path.join(HERE, "agare", k + ".json")
        if os.path.exists(fil):
            s = json.load(open(fil, encoding="utf-8"))
            if "owner_name" in s:
                svar.append(s)
    butiker(data_dir, ut, svar)
    return True


# Kontovärdet timme för timme. Serverns eget lager; hourly.json i sajten är en
# kopia utanför git, som live.json. Bara totalen per timme publiceras, inga
# priser per föremål (API-villkoren: ingen återpublicering i bulk).
TIMMAR = os.path.join(HERE, "timmar.json")
OMRAKNA_TIMMAR = 48  # varvet över föremålen tar ~9 h; äldre timmar är färdiga


def timvarde(historik, updates, t):
    """Kontovärdet vid t (slutet av en timme), räknat som dagspunkten: samma
    fönster och tvådagsregel mot gårdagens punkt, dagens antal. Ett föremål
    vars historik slutar före t behåller priset där den slutar; utan historik
    gäller senaste punktens pris. Vid midnatt blir värdet dagens punkt."""
    dag = datetime.datetime.fromtimestamp(t - 1, SVERIGE).date().isoformat()
    fore = [u for u in updates if u["date"] < dag]
    egen = next((u for u in updates if u["date"] == dag), None)
    forra = fore[-1] if fore else None
    qty = (egen or forra or updates[0])["qty"]
    v = 0
    for i, q in qty.items():
        p, h = None, historik.get(i)
        if h:
            g = forra and {"w": forra.get("basis", {}).get(i), "q": forra.get("q", {}).get(i),
                           "p": forra["prices"].get(i)}
            m = markera(h, min(t, h[-1]["hour"] + 3600), g if g and g["w"] else None)
            p = m and m["p"]
        if p is None:
            p = ((egen or forra or {}).get("prices") or {}).get(i)
        v += (p or 0) * q
    return round(v)


def timmar(data_dir, ut_fil, fran=None):
    """Räknar om timmarna från fran (--bakat: från första dagen), annars de
    senaste OMRAKNA_TIMMAR; äldre timmar i lagret står kvar. Skriver lagret
    och ut_fil: {"updated", "points": [[t, värde], ...]}, t i sekunder."""
    data = json.load(open(os.path.join(data_dir, "prices.json"), encoding="utf-8"))
    if data.get("source") != "maplestory.market":
        return
    upp = data["updates"]
    gamla = json.load(open(TIMMAR, encoding="utf-8"))["points"] if os.path.exists(TIMMAR) else []
    historik = {}
    for u in upp:
        for i in u["qty"]:
            fil = os.path.join(HISTORIK, mid(i) + ".json")
            if i not in historik and os.path.exists(fil):
                h = json.load(open(fil, encoding="utf-8"))
                if h:
                    historik[i] = h
    if not historik:
        return
    slut = max(h[-1]["hour"] for h in historik.values()) + 3600
    if fran is None:
        forsta = datetime.date.fromisoformat(upp[0]["date"])
        fran = slut - OMRAKNA_TIMMAR * 3600 if gamla else matpunkt(forsta - datetime.timedelta(days=1)) + 3600
    # Historiken växer (hamta_historik slår ihop); fönstren går högst en vecka bakåt.
    historik = {i: [r for r in h if r["hour"] >= fran - 8 * 86400] or h[-1:] for i, h in historik.items()}
    punkter = [p for p in gamla if p[0] < fran]
    punkter += [[t, timvarde(historik, upp, t)] for t in range(fran, slut + 1, 3600)]
    ut = {"updated": int(datetime.datetime.now(SVERIGE).timestamp()), "points": punkter}
    skriv_json(TIMMAR, ut)
    if ut_fil:
        skriv_json(ut_fil, ut)
    return punkter


def live(data_dir, ut_fil):
    """live.json: varje föremåls pris räknat ur den senast hämtade
    historiken, med fönstret slutande där historiken slutar. Skrivs varje
    timme direkt i sajten, utanför git; sidan visar det som kontots värde
    just nu, och dygnets sista livevärde blir dagens punkt i prices.json.
    Gårdagens punkt ger fönstret för tvådagarsregeln."""
    data = json.load(open(os.path.join(data_dir, "prices.json"), encoding="utf-8"))
    if data.get("source") != "maplestory.market":
        return  # före --bakat finns ingen historik att räkna på
    # Antalen ur sista posten (kan vara synkens), fönstret och tvådagsregeln
    # ur sista riktiga dagspunkten.
    antal = data["updates"][-1]["qty"]
    stang = [u for u in data["updates"] if not u.get("inventory")][-1]
    items = {}
    for i, q in antal.items():
        fil = os.path.join(HISTORIK, mid(i) + ".json")
        if not os.path.exists(fil):
            continue
        h = json.load(open(fil, encoding="utf-8"))
        if not h:
            continue
        t = h[-1]["hour"] + 3600
        h = [r for r in h if r["hour"] >= t - 9 * 86400]  # fönstren går en vecka bakåt, från t − 24 h
        forra = {"w": stang.get("basis", {}).get(i), "q": stang.get("q", {}).get(i), "p": stang["prices"].get(i)}
        m = markera(h, t, forra if forra["w"] else None)
        if m:
            items[i] = {"p": m["p"], "w": m["w"], "q": m["q"], "at": t, **likviditet(h, t, q)}
            # Rörelsen senaste 24 h (sidans sortering "24 hours"): samma fönster
            # 24 h tidigare, så att ett fönsterbyte inte blir en rörelse. Bara
            # procenten publiceras, inte det gamla priset.
            fore = fonsterpris(h, t - 86400, m["w"]) if m["w"] != "stale" else None
            items[i]["c24"] = round((m["p"] / fore - 1) * 100, 1) if fore else None
    skriv_json(ut_fil, {"updated": int(datetime.datetime.now(SVERIGE).timestamp()), "items": items})
    print("live: %d föremål, %s mesos" % (len(items), format(sum(v["p"] * antal[i] for i, v in items.items()), ",")))


def main(args):
    sys.stdout.reconfigure(encoding="utf-8")
    web = args[args.index("--web") + 1] if "--web" in args else inst.WEB
    if "--live" in args:
        live_ut = args[args.index("--live-ut") + 1]
        live(inst.data(web), live_ut)
        timmar(inst.data(web), os.path.join(os.path.dirname(live_ut), "hourly.json"))
        return
    remotes = args[args.index("--remotes") + 1].split(",") if "--remotes" in args else inst.REMOTES
    data_dir = inst.data(web)
    ut = args[args.index("--ut") + 1] if "--ut" in args else data_dir
    push = "--ingen-push" not in args and "--ut" not in args
    git = ["git", "-C", web]
    if push:
        subprocess.run(git + ["pull", "--rebase", "--autostash", "-q", remotes[0], "main"], check=True)
    if "--bakat" in args:
        bakat(data_dir, ut)
    elif not idag(data_dir, ut, args[args.index("--live-ut") + 1] if "--live-ut" in args
                  else os.path.join(data_dir, "live.json")):
        return
    if not push:
        return
    subprocess.run(git + ["add", inst.UNDERMAPP], check=True)
    if subprocess.run(git + ["diff", "--cached", "--quiet"]).returncode == 0:
        print("inget nytt")
        return
    meddelande = "MapleStory prices from maplestory.market"
    subprocess.run(git + ["commit", "-q", "-m", meddelande], check=True)
    for r in remotes:
        subprocess.run(git + ["push", "-q", r, "main"], check=True)


if __name__ == "__main__":
    main(sys.argv[1:])
