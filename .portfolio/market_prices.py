"""Prices from maplestory.market for the page.

  python market_prices.py --rebuild [--out <folder>] [--no-push]
      Rebuilds the whole of prices.json from history/ (fetch_history.py), one
      point per day from a week before the first inventory until today (as
      far back as the API's history goes). The events' market price in
      activity.json is worked out the same way.

  python market_prices.py --live --live-out <file> [--web <site checkout>]
      The live prices from the history there is, every hour (run_hourly.sh
      on a server, actions_job.py on GitHub).

  python market_prices.py [--web <site checkout>] [--remotes origin] [--no-push]
      The first run after midnight saves yesterday's last live value as
      that day's point in prices.json. No closing time: the game runs round
      the clock.

The price is the unit-weighted median of the hours' sold medians, in the
shortest window (24h, 72h, 7d) with enough sales; see price_at() and
WINDOW_RULES. Back in time the same measure is taken from the hourly
history, with the end of the day (midnight in the page's time zone) as the
point of measurement.

The days before the first inventory are valued with that inventory's
counts. Such points get "assumed_qty": true, and the page draws them dotted.

Git: pulls (pull --rebase) before anything is written, since both the
server and the computer with the game commit to the same repo.

The API terms (maplestory.market/docs/api/terms): the credit and the links
are on the page, the key stays in api_key.txt, and the answers are saved
instead of fetched again. Only percentages and totals are published from
the history, never old prices per item in bulk.
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

import settings as cfg  # noqa: E402
from fetch_history import CHARACTERS, TO_MARKET, api_key  # noqa: E402

HISTORY_DIR = os.path.join(HERE, "history")
TZ = cfg.TIME_ZONE  # the page's time zone; the day ends at midnight there
# A day may not move the prices more than this without someone looking. It
# was 0.25; one day the account really rose ~29 % (a Founder crate left the
# Cash Shop and two items doubled).
MAX_JUMP = 0.40


def market_id(iid):
    return TO_MARKET.get(iid, iid)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


def total(u):
    return sum(u["prices"].get(i, 0) * q for i, q in u["qty"].items())


def check(updates):
    """Stops before anything is pushed if the total jumps unreasonably
    between two days. An item without a price (never seen selling) is only a
    warning; the page shows it as "no recorded price"."""
    errors = []
    for u in updates:
        missing = [i for i in u["qty"] if i not in u["prices"]]
        if missing:
            print("warning: %s has no price for %s" % (u["date"], ", ".join(missing)))
    for a, b in zip(updates, updates[1:]):
        # The price movement on b's counts: buys and sales are no jump.
        both = [i for i in b["qty"] if i in a["prices"] and i in b["prices"]]
        before = sum(a["prices"][i] * b["qty"][i] for i in both)
        after = sum(b["prices"][i] * b["qty"][i] for i in both)
        if before and abs(after / before - 1) > MAX_JUMP:
            errors.append("the prices jump %s -> %s: %d -> %d on the same counts" % (a["date"], b["date"], before, after))
    if errors:
        sys.exit("STOPPED, nothing written:\n  " + "\n  ".join(errors))


def day_end(day):
    """The end of the day (midnight in the page's time zone) in unix seconds:
    a day's point in the history is the day's last value. No closing time;
    the game and the page run round the clock."""
    d = day + datetime.timedelta(days=1)
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


# ── The valuation: high volume = a shorter window ──
# Windows, shortest first: (name, hours, at least units sold, at least hours with sales).
# The demand for spread-out hours keeps a single buyer from setting the price.
# The 24h demand was lowered from 20 units / 6 hours to 10/5: better a daily
# price that moves. On a day when the market recorded a sixth of the day,
# with empty hours, nine kinds that usually sell 30-190 a day fell to 72h;
# with 10/5, 41 of 43 made it that day. The cap (25 % per hour) and the
# demand for five hours still keep a single buyer out.
WINDOW_RULES = [("24h", 24, 10, 5), ("72h", 72, 8, 4), ("7d", 168, 3, 2)]
ORDER = {"24h": 0, "72h": 1, "7d": 2, "stale": 3}
CAP = 0.25          # no single hour weighs more than this in the median
LIQUID_SHARE = 0.2  # the share of a day's sales one seller can take without pushing the price


def weighted_median(pairs):
    """pairs = [(value, weight)]; the value where half the weight is passed."""
    pairs = sorted(pairs)
    half = sum(w for _, w in pairs) / 2
    acc = 0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return None


def hours_with_sales(hours, t, h):
    return [r for r in hours if t - h * 3600 <= r["hour"] < t and r.get("sold")]


def qualifies(hours, t):
    """The shortest window that meets its demand, else "stale"."""
    for name, h, min_units, min_hours in WINDOW_RULES:
        rows = hours_with_sales(hours, t, h)
        if sum(r["sold"]["units"] for r in rows) >= min_units and len(rows) >= min_hours:
            return name
    return "stale"


def window_price(hours, t, w):
    """The price at t in the window w ("24h", "72h", "7d"), or None."""
    h = dict((n, hh) for n, hh, _, _ in WINDOW_RULES)[w]
    # Odd hours are compared with the window's typical hour (the unweighted
    # median of the hours' medians), not the week's: an item that went from
    # 220k to 450k in a day would otherwise have had its new trades filtered
    # out. A junk hour (103 units at 481 million) weighs one hour, not 103 units.
    window = hours_with_sales(hours, t, h)
    if not window:
        return None
    typical = statistics.median(r["sold"]["median_unit_price"] for r in window)
    rows = [r for r in window if 0.5 * typical <= r["sold"]["median_unit_price"] <= 2 * typical]
    total_units = sum(r["sold"]["units"] for r in rows)
    pairs = [(r["sold"]["median_unit_price"], min(r["sold"]["units"], CAP * total_units)) for r in rows]
    return weighted_median(pairs)


def price_at(hours, t, previous=None):
    """An item's price for the day at time t.

    The value is the unit-weighted median of the hours' sold medians in the
    chosen window: an odd trade moves an average but not a median. Hours
    outside 0.5-2 × the window's typical hour don't count, and no hour weighs
    more than CAP. The window changes to a shorter one only when the shorter
    one has held two days running (previous = yesterday's {"w", "q", "p"});
    otherwise the change itself makes false daily movements. If nothing has
    sold in a week: the lower of yesterday's price and 85 % of the asking
    price, marked "stale".

    Returns {"p": price, "w": window used, "q": shortest that qualified
    today} or None."""
    q = qualifies(hours, t)
    w = q
    if previous and ORDER[q] < ORDER.get(previous.get("w"), 3):
        # Shorter than yesterday: only as short as both today and yesterday manage.
        name = {v: k for k, v in ORDER.items()}
        w = name[min(max(ORDER[q], ORDER.get(previous.get("q"), 3)), ORDER[previous["w"]])]
        if w == "stale":
            w = q  # sales again after an empty week: take them at once
    if w != "stale":
        p = window_price(hours, t, w)
        if p:
            return {"p": round(p), "w": w, "q": q}
    # Nothing sold in a week: the asking prices are a ceiling, not a value.
    asking = sorted(r["seen"]["median_unit_price"] for r in hours
                    if t - 24 * 3600 <= r["hour"] < t and r.get("seen"))
    candidates = [x for x in ((previous or {}).get("p"), asking[len(asking) // 2] * 0.85 if asking else None) if x]
    return {"p": round(min(candidates)), "w": "stale", "q": q} if candidates else None


def liquidity(hours, t, count):
    """Sold a day (the week's average) and the days to sell the holding if
    you take a fifth of the flow."""
    # Junk hours out: an hour with 103 units at 481 million would otherwise
    # have doubled "sold a day". Wide (0.2-5 × the week's typical hour), so
    # that a real price movement during the week counts.
    week = hours_with_sales(hours, t, 168)
    typical = statistics.median(r["sold"]["median_unit_price"] for r in week) if week else 0
    units = sum(r["sold"]["units"] for r in week if 0.2 * typical <= r["sold"]["median_unit_price"] <= 5 * typical)
    days = min(7, max(1, (t - hours[0]["hour"]) / 86400)) if hours else 7
    per_day = units / days
    return {"per_day": round(per_day, 1),
            "days_to_sell": round(count / (LIQUID_SHARE * per_day), 1) if per_day else None}


METHOD = ("unit-weighted median of hourly sold medians; window 24h/72h/7d by volume "
          "(10 units in 5 hours / 8 in 4 / 3 in 2), shorter window only after 2 days; "
          "no sales in 7 days: min(previous, 0.85 x asking median)")


def day_point(history, qty, t, previous):
    """A day's point: price, window used ("basis") and the shortest window
    that qualified ("q", for the two-day rule). previous = yesterday's point."""
    prices, basis, qual = {}, {}, {}
    for i in qty:
        g = previous and {"w": previous.get("basis", {}).get(i), "q": previous.get("q", {}).get(i),
                          "p": previous["prices"].get(i)}
        # If the history ends before t, the price is the one where it ends (as
        # live and the hours do), not a window with empty hours at the end.
        h = history[i]
        m = price_at(h, min(t, h[-1]["hour"] + 3600) if h else t, g if g and g["w"] else None)
        if m:
            prices[i], basis[i], qual[i] = m["p"], m["w"], m["q"]
    return {"prices": prices, "qty": qty, "basis": basis, "q": qual}


def write_market(out, point, liq, price_rows):
    """market.json: liquidity and asking premium per item, for the cards
    (once a day; live.json has the liquidity hour by hour). liq = {id:
    {"per_day", "days_to_sell"}}. Asking premium = (p25 of the asking prices
    − our price) / our price; negative means the sellers already ask below
    the latest trades."""
    p25 = {str(r["item_id"]): r["seen"]["p25_unit_price"] for r in price_rows if r.get("seen")}
    items = {}
    for i, q in point["qty"].items():
        if i not in point["prices"]:
            continue
        row = {k: liq.get(i, {}).get(k) for k in ("per_day", "days_to_sell")}
        row["basis"] = point["basis"].get(i)
        if p25.get(market_id(i)):
            row["ask_premium"] = round((p25[market_id(i)] / point["prices"][i] - 1) * 100, 1)
        items[i] = row
    write_json(os.path.join(out, "market.json"), {"date": point["date"], "items": items})


def read_history(iid):
    path = os.path.join(HISTORY_DIR, market_id(iid) + ".json")
    if not os.path.exists(path):
        sys.exit("no history for %s (%s); let fetch_history.py finish first" % (iid, market_id(iid)))
    return json.load(open(path, encoding="utf-8"))


def rebuild(data_dir, out):
    old = json.load(open(os.path.join(data_dir, "prices.json"), encoding="utf-8"))["updates"]
    # The counts per date: each inventory holds until the next.
    counts = [(u["date"], u["qty"]) for u in old if u.get("qty") and not u.get("assumed_qty")]
    INVENTORY, first_counts = counts[0]
    # As far back as the API's history goes (a week).
    FIRST_DAY = datetime.date.fromisoformat(INVENTORY) - datetime.timedelta(days=6)
    history = {}
    for _, q in counts:
        for i in q:
            if i not in history:
                history[i] = read_history(i)

    today = datetime.date.today()
    now = datetime.datetime.now(TZ).timestamp()
    updates = []
    day = FIRST_DAY
    while day <= today and day_end(day) <= now:  # today's point comes after midnight
        d = day.isoformat()
        qty = first_counts
        for dd, q in counts:
            if dd <= d:
                qty = q
        point = day_point(history, qty, day_end(day), updates[-1] if updates else None)
        point["date"] = d
        if d < INVENTORY:
            point["assumed_qty"] = True
        updates.append(point)
        day += datetime.timedelta(days=1)
    check(updates)
    t = day_end(day - datetime.timedelta(days=1))
    try:
        rows = api("prices")  # the asking prices, for the asking premium
    except OSError as e:
        print("could not fetch the asking prices: %s" % e)
        rows = []
    write_market(out, updates[-1], {i: liquidity(history[i], t, q) for i, q in updates[-1]["qty"].items()}, rows)
    # An inventory today (the day isn't over): like sync.py, an entry with
    # yesterday's prices and the new counts. The night after it is replaced
    # by a real day point.
    later = [u for u in old if u["date"] >= day.isoformat() and u.get("qty")]
    if later:
        q = later[-1]["qty"]
        updates.append({"date": later[-1]["date"], "inventory": True, "qty": q,
                        "prices": {i: p for i, p in updates[-1]["prices"].items() if i in q}})
    write_json(os.path.join(out, "prices.json"), {"source": "maplestory.market", "method": METHOD, "updates": updates})

    # The events' "price" is the market price that day: what the trading and
    # the breakdown under the account value count against.
    path = os.path.join(data_dir, "activity.json")
    act = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"events": []}
    for e in act["events"]:
        h = history.get(e["id"]) or read_history(e["id"])
        m = price_at(h, day_end(datetime.date.fromisoformat(e["date"])))
        if m:
            e["price"] = m["p"]
    write_json(os.path.join(out, "activity.json"), act)

    for u in updates:
        print(u["date"], "%10s" % format(total(u), ","), "assumed counts" if u.get("assumed_qty") else "")
    # The hours from the first day, in the store (hours.json); the hourly job takes over from there.
    if out == data_dir:
        p = hours(data_dir, os.path.join(out, "hourly.json"),
                  start=day_end(FIRST_DAY - datetime.timedelta(days=1)) + 3600)
        if not p:
            return
        print("%d hours up to %s" % (len(p), datetime.datetime.fromtimestamp(p[-1][0], TZ).strftime("%d %b %H:%M")))


def api(api_path):
    req = urllib.request.Request("https://maplestory.market/api/" + api_path,
                                 headers={"Authorization": "Bearer " + api_key(),
                                          "User-Agent": cfg.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as answer:
        return json.load(answer)


def shops(data_dir, out, answers):
    """shops.json: everything the characters' shops have sold. /api/owners
    only shows the latest shops, so every shop (player_id) is kept here and
    the total grows instead of losing old shops. answers = the owners answers."""
    path = os.path.join(data_dir, "shops.json")
    data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"openings": {}}
    for s in answers:
        for o in s.get("openings", []):
            data["openings"][str(o["player_id"])] = {
                "owner": s["owner_name"], "name": o["name"], "opened_at": o["opened_at"],
                "last_seen": o["last_seen"], "sales": o["sales"]["sales"],
                "units": o["sales"]["units"], "mesos": round(o["sales"]["mesos"])}
    data["updated"] = datetime.datetime.now(TZ).date().isoformat()
    data["source"] = "maplestory.market"
    write_json(os.path.join(out, "shops.json"), data)


def yesterday_point(data_dir, out, live_file):
    """Yesterday's point in the history: the day's last live value (live.json
    from the run just before midnight). Runs every hour; does something only
    the first time after midnight, when yesterday is missing. One commit a day."""
    now = datetime.datetime.now(TZ)
    data = json.load(open(os.path.join(data_dir, "prices.json"), encoding="utf-8"))
    updates = data["updates"]
    d = (now.date() - datetime.timedelta(days=1)).isoformat()
    # An entry with inventory: true (sync.py: yesterday's prices, new counts)
    # is no day point; if it is for yesterday it is replaced, with its counts.
    synced = updates[-1] if updates[-1].get("inventory") and updates[-1]["date"] == d else None
    if updates[-1]["date"] >= d and not synced:
        return False
    if data.get("source") != "maplestory.market":
        print("prices.json is not built from the market's history yet (fetch_history.py "
              "--then-publish, see the README); no day point")
        return False
    if not os.path.exists(live_file):
        sys.exit("live.json is missing")
    lv = json.load(open(live_file, encoding="utf-8"))
    if now.timestamp() - lv["updated"] > 3 * 3600:
        sys.exit("live.json is more than three hours old")
    if synced:
        updates.pop()
    last = updates[-1]
    point = {"date": d, "prices": dict(last["prices"]), "qty": (synced or last)["qty"],
             "basis": dict(last.get("basis", {})), "q": dict(last.get("q", {}))}
    for i, x in lv["items"].items():
        if i in point["qty"]:
            point["prices"][i], point["basis"][i], point["q"][i] = x["p"], x["w"], x["q"]
    updates.append(point)
    check(updates[-2:])
    write_json(os.path.join(out, "prices.json"), data)
    # hourly.json is written by --live every hour, not here: with an own web
    # server it is outside git, otherwise every push would reset it (README).
    print(d, format(total(point), ","))
    # The asking prices (p25) for the asking premium: one call. If the
    # hour's calls are used up, the prices are published anyway, without it.
    try:
        rows = api("prices")
        os.makedirs(os.path.join(HERE, "daily"), exist_ok=True)
        with open(os.path.join(HERE, "daily", "prices_%s.json" % d), "w", encoding="utf-8") as f:
            json.dump(rows, f)
    except urllib.error.HTTPError as e:
        print("could not fetch the asking prices: %s" % e.code)
        rows = []
    write_market(out, point, {i: x for i, x in lv["items"].items()}, rows)
    # The shops: the answers the rotation fetched (404 = never seen with a shop).
    answers = []
    for k in CHARACTERS:
        path = os.path.join(HERE, "owners", k + ".json")
        if os.path.exists(path):
            s = json.load(open(path, encoding="utf-8"))
            if "owner_name" in s:
                answers.append(s)
    shops(data_dir, out, answers)
    return True


# The account value hour by hour. The job's own store; hourly.json on the site
# is a copy outside git, like live.json. Only the total per hour is published,
# no prices per item (the API terms: no republishing in bulk).
HOURS = os.path.join(HERE, "hours.json")
RECALC_HOURS = 48  # the round over the items takes ~9 h; older hours are final


def hour_value(history, updates, t):
    """The account value at t (the end of an hour), worked out like the day
    point: the same window and two-day rule against yesterday's point, the
    day's counts. An item whose history ends before t keeps the price where
    it ends; without history the latest point's price holds. At midnight the
    value becomes the day's point."""
    day = datetime.datetime.fromtimestamp(t - 1, TZ).date().isoformat()
    before = [u for u in updates if u["date"] < day]
    own = next((u for u in updates if u["date"] == day), None)
    previous = before[-1] if before else None
    qty = (own or previous or updates[0])["qty"]
    v = 0
    for i, q in qty.items():
        p, h = None, history.get(i)
        if h:
            g = previous and {"w": previous.get("basis", {}).get(i), "q": previous.get("q", {}).get(i),
                              "p": previous["prices"].get(i)}
            m = price_at(h, min(t, h[-1]["hour"] + 3600), g if g and g["w"] else None)
            p = m and m["p"]
        if p is None:
            p = ((own or previous or {}).get("prices") or {}).get(i)
        v += (p or 0) * q
    return round(v)


def hours(data_dir, out_file, start=None):
    """Works out the hours again from start (--rebuild: from the first day),
    else the latest RECALC_HOURS; older hours in the store stay. Writes the
    store and out_file: {"updated", "points": [[t, value], ...]}, t in seconds."""
    data = json.load(open(os.path.join(data_dir, "prices.json"), encoding="utf-8"))
    if data.get("source") != "maplestory.market":
        return
    updates = data["updates"]
    old = json.load(open(HOURS, encoding="utf-8"))["points"] if os.path.exists(HOURS) else []
    history = {}
    for u in updates:
        for i in u["qty"]:
            path = os.path.join(HISTORY_DIR, market_id(i) + ".json")
            if i not in history and os.path.exists(path):
                h = json.load(open(path, encoding="utf-8"))
                if h:
                    history[i] = h
    if not history:
        return
    end = max(h[-1]["hour"] for h in history.values()) + 3600
    if start is None:
        first = datetime.date.fromisoformat(updates[0]["date"])
        start = end - RECALC_HOURS * 3600 if old else day_end(first - datetime.timedelta(days=1)) + 3600
    # The history grows (fetch_history merges); the windows go at most a week back.
    history = {i: [r for r in h if r["hour"] >= start - 8 * 86400] or h[-1:] for i, h in history.items()}
    points = [p for p in old if p[0] < start]
    points += [[t, hour_value(history, updates, t)] for t in range(start, end + 1, 3600)]
    result = {"updated": int(datetime.datetime.now(TZ).timestamp()), "points": points}
    write_json(HOURS, result)
    if out_file:
        write_json(out_file, result)
    return points


def live(data_dir, out_file):
    """live.json: every item's price worked out from the latest fetched
    history, with the window ending where the history ends. Written every
    hour straight into the site; the page shows it as the account's value
    now, and the day's last live value becomes the day's point in
    prices.json. Yesterday's point gives the window for the two-day rule."""
    data = json.load(open(os.path.join(data_dir, "prices.json"), encoding="utf-8"))
    if data.get("source") != "maplestory.market":
        return  # before --rebuild there is no history to work from
    # The counts from the last entry (may be the sync's), the window and the
    # two-day rule from the last real day point.
    counts = data["updates"][-1]["qty"]
    closed = [u for u in data["updates"] if not u.get("inventory")][-1]
    items = {}
    for i, q in counts.items():
        path = os.path.join(HISTORY_DIR, market_id(i) + ".json")
        if not os.path.exists(path):
            continue
        h = json.load(open(path, encoding="utf-8"))
        if not h:
            continue
        t = h[-1]["hour"] + 3600
        h = [r for r in h if r["hour"] >= t - 9 * 86400]  # the windows go a week back, from t − 24 h
        previous = {"w": closed.get("basis", {}).get(i), "q": closed.get("q", {}).get(i), "p": closed["prices"].get(i)}
        m = price_at(h, t, previous if previous["w"] else None)
        if m:
            items[i] = {"p": m["p"], "w": m["w"], "q": m["q"], "at": t, **liquidity(h, t, q)}
            # The movement over the last 24 h (the page's "24 hours" sort): the
            # same window 24 h earlier, so that a change of window is no
            # movement. Only the percentage is published, not the old price.
            before = window_price(h, t - 86400, m["w"]) if m["w"] != "stale" else None
            items[i]["c24"] = round((m["p"] / before - 1) * 100, 1) if before else None
    write_json(out_file, {"updated": int(datetime.datetime.now(TZ).timestamp()), "items": items})
    print("live: %d items, %s mesos" % (len(items), format(sum(v["p"] * counts[i] for i, v in items.items()), ",")))


def arg(args, *names):
    """The value after the first of the flags given (English or the older Swedish)."""
    for n in names:
        if n in args:
            return args[args.index(n) + 1]
    return None


def main(args):
    sys.stdout.reconfigure(encoding="utf-8")
    web = arg(args, "--web") or cfg.WEB
    live_out = arg(args, "--live-out", "--live-ut")
    if "--live" in args:
        live(cfg.data_dir(web), live_out)
        hours(cfg.data_dir(web), os.path.join(os.path.dirname(live_out), "hourly.json"))
        return
    remotes = arg(args, "--remotes").split(",") if "--remotes" in args else cfg.REMOTES
    data_dir = cfg.data_dir(web)
    out = arg(args, "--out", "--ut") or data_dir
    push = "--no-push" not in args and "--ingen-push" not in args and out == data_dir
    git = ["git", "-C", web]
    if push:
        subprocess.run(git + ["pull", "--rebase", "--autostash", "-q", remotes[0], "main"], check=True)
    if "--rebuild" in args or "--bakat" in args:
        rebuild(data_dir, out)
    elif not yesterday_point(data_dir, out, live_out or os.path.join(data_dir, "live.json")):
        return
    if not push:
        return
    subprocess.run(git + ["add", cfg.SUBFOLDER], check=True)
    if subprocess.run(git + ["diff", "--cached", "--quiet"]).returncode == 0:
        print("nothing new")
        return
    subprocess.run(git + ["commit", "-q", "-m", "MapleStory prices from maplestory.market"], check=True)
    for r in remotes:
        subprocess.run(git + ["push", "-q", r, "main"], check=True)


if __name__ == "__main__":
    main(sys.argv[1:])
