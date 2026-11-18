"""Fetches the hourly price history of every item that has ever been in the Deco tab.

  python fetch_history.py [--web <site checkout>] [--then-publish]

  python fetch_history.py --rotate --web <site checkout>   (every hour, run_hourly.sh)

--then-publish runs market_prices.py --rebuild when everything is fetched,
which rebuilds prices.json and pushes it. The first time: see the README.

One file per item in history/<market id>.json. What is already fetched is
skipped, so the script can be stopped and started again. The key is in
api_key.txt next to it, never in the site repo (the API terms: never in the
page's code).

The key gets 6 calls an hour. The script reads x-ratelimit-remaining and
x-ratelimit-reset in every answer and waits for the next hour when they are
used up. The history goes a week back.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import settings as cfg

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(HERE, "history")
PRICES = os.path.join(cfg.data_dir(), "prices.json")
# Our client's id -> the market's, where they differ.
TO_MARKET = {"1112100": "1112222"}  # White Label Ring


def api_key():
    return open(os.path.join(HERE, "api_key.txt"), encoding="utf-8").read().strip()


def all_ids(prices=PRICES, only_now=False):
    """The market id of everything ever owned, or with only_now only what is
    owned now (the last entry's counts)."""
    updates = json.load(open(prices, encoding="utf-8"))["updates"]
    ids = []
    for u in updates[-1:] if only_now else updates:
        for i in (list(u.get("qty", {})) if only_now else list(u["prices"]) + list(u.get("qty", {}))):
            m = TO_MARKET.get(i, i)
            if m not in ids:
                ids.append(m)
    return ids


# Calls per hour for the rotation; the sixth is left for the sync and the like.
PER_HOUR = 5


def rotate(prices):
    """The hourly job: fetches the history of the PER_HOUR items fetched
    longest ago, round the clock. A round over 43 items takes about nine
    hours, so no item's price is older than that. The game runs round the
    clock, and so does the rotation. The characters' shops are fetched once
    a day; a character that answered 404 (never seen with a shop) only once
    a week."""
    def age(path):
        return os.path.getmtime(path) if os.path.exists(path) else 0
    # Only once the prices rest on the market's history (after --rebuild);
    # before that the rotation would share the key's calls with the first fetch.
    if json.load(open(prices, encoding="utf-8")).get("source") != "maplestory.market":
        return
    now = time.time()
    queue = [("items/%s/history" % i, os.path.join(HISTORY_DIR, i + ".json")) for i in all_ids(prices, only_now=True)]
    for s, f in EXTRA:
        no_shop = os.path.exists(f) and '"error": 404' in open(f, encoding="utf-8").read(200)
        if now - age(f) > (7 * 86400 if no_shop else 20 * 3600):
            queue.append((s, f))
    queue.sort(key=lambda sf: age(sf[1]))
    for api_path, path in queue[:PER_HOUR]:
        data, h = fetch(api_path)
        if data is None:
            break  # 429: this hour's calls are used up
        save(path, data)
        print("%s  %s" % (time.strftime("%H:%M"), api_path), flush=True)
        if int(h.get("x-ratelimit-remaining", 1)) <= 0:
            break


# Other calls that go in the same queue: (path in the API, file here).
CHARACTERS = cfg.CHARACTERS
# The latest answer per character; earlier days' answers stay in shops.json.
EXTRA = [("owners/" + k, os.path.join(HERE, "owners", "%s.json" % k)) for k in CHARACTERS]


def save(path, data):
    """Writes an answer. The history is merged with what is already there,
    hour by hour (the new answer wins), since the API only gives a week
    back: overwriting would throw away everything older. About 2 MB a week
    for 43 items."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.dirname(path) == HISTORY_DIR and os.path.exists(path):
        try:
            rows = {r["hour"]: r for r in json.load(open(path, encoding="utf-8"))}
            rows.update({r["hour"]: r for r in json.loads(data)})
            data = json.dumps([rows[k] for k in sorted(rows)], separators=(",", ":")).encode()
        except (ValueError, KeyError, TypeError):
            pass  # an unexpected format: save the answer as it is, rather than stop
    with open(path, "wb") as f:
        f.write(data)


def fetch(api_path):
    req = urllib.request.Request(
        "https://maplestory.market/api/" + api_path,
        headers={"Authorization": "Bearer " + api_key(), "User-Agent": cfg.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as answer:
            return answer.read(), answer.headers
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, e.headers
        if e.code == 404 and api_path.startswith("items/"):
            return b"[]", e.headers  # the market has never seen the item
        if api_path.startswith("owners/"):
            # A character without a shop must not stop the price fetch.
            return json.dumps({"error": e.code, "body": e.read()[:500].decode("utf-8", "replace")}).encode(), e.headers
        stop(api_path, e)


def stop(what, e):
    print("ERROR %s: %s %s" % (what, e.code, e.read()[:200]), flush=True)
    sys.exit(1)


def wait_until(reset):
    s = max(0, int(reset) - time.time()) + 30
    print("  waiting %d min" % (s // 60), flush=True)
    time.sleep(s)


def main(args):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    web = args[args.index("--web") + 1] if "--web" in args else cfg.WEB
    if "--rotate" in args or "--rotera" in args:
        rotate(os.path.join(cfg.data_dir(web), "prices.json"))
        return
    prices = os.path.join(cfg.data_dir(web), "prices.json")
    left = [(s, f) for s, f in EXTRA if not os.path.exists(f)] + [
        ("items/%s/history" % i, os.path.join(HISTORY_DIR, i + ".json")) for i in all_ids(prices)
        if not os.path.exists(os.path.join(HISTORY_DIR, i + ".json"))]
    print("%d calls left" % len(left), flush=True)
    while left:
        api_path, path = left[0]
        data, h = fetch(api_path)
        if data is None:  # 429: wait and try again
            wait_until(h.get("x-ratelimit-reset") or time.time() + int(h.get("Retry-After", 600)))
            continue
        save(path, data)
        left.pop(0)
        print("%s  %s  %d left" % (time.strftime("%H:%M"), api_path, len(left)), flush=True)
        if left and int(h.get("x-ratelimit-remaining", 1)) <= 0:
            wait_until(h["x-ratelimit-reset"])
    print("done", flush=True)
    if "--then-publish" in args or "--sen-publicera" in args:
        import market_prices
        market_prices.main(["--rebuild", "--web", web])


if __name__ == "__main__":
    main(sys.argv[1:])
