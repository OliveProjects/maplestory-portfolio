"""The hourly price job when it runs on GitHub Actions (the workflow that
website_creator.py adds to your site repo) instead of on your own server.

It lives in .portfolio/ in the site repo, next to the scripts it calls, and
does what run_hourly.sh does on a server, with one difference: Actions cannot wait
for hours, so the first fetch of every item's history is spread over the
hourly runs, five calls per hour, and the first price build (--rebuild) runs
by itself once everything is fetched.

The API key comes from the repo secret MAPLESTORY_MARKET_KEY, written to
api_key.txt by the workflow and deleted after the run. The fetched history
(history/, owners/, daily/, hours.json) is kept in the Actions cache and
never committed: maplestory.market's terms do not allow republishing their
data in bulk.

    python actions_job.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import fetch_history as fh  # noqa: E402
import settings as cfg  # noqa: E402

# The site's root: by default the folder above .portfolio/ (the repo on
# Actions). On your own server, set PORTFOLIO_WEB to the folder the web
# server shows, and keep the scripts and api_key.txt outside it.
WEB = os.path.abspath(os.environ.get("PORTFOLIO_WEB") or os.path.join(HERE, ".."))
DATA = cfg.data_dir(WEB)
PRICES = os.path.join(DATA, "prices.json")
LIVE = os.path.join(DATA, "live.json")


def run(*args):
    print("> python " + " ".join(args), flush=True)
    return subprocess.run([sys.executable] + list(args)).returncode


def first_fetch():
    """Fetches up to five missing histories. True when nothing is missing."""
    os.makedirs(fh.HISTORY_DIR, exist_ok=True)

    def missing():
        return [(s, f) for s, f in fh.EXTRA if not os.path.exists(f)] + [
            ("items/%s/history" % i, os.path.join(fh.HISTORY_DIR, i + ".json")) for i in fh.all_ids(PRICES)
            if not os.path.exists(os.path.join(fh.HISTORY_DIR, i + ".json"))]

    left = missing()
    print("First fetch: %d calls left (5 per hour)" % len(left), flush=True)
    for api_path, path in left[:fh.PER_HOUR]:
        data, h = fh.fetch(api_path)
        if data is None:
            print("429: this hour's calls are used up", flush=True)
            break
        fh.save(path, data)
        print("  " + api_path, flush=True)
        if int(h.get("x-ratelimit-remaining", 1)) <= 0:
            break
    return not missing()


def main():
    if not os.path.exists(PRICES):
        print("No prices.json yet: run the first sync on your computer (Sync.bat).")
        return
    source = json.load(open(PRICES, encoding="utf-8")).get("source")
    if source != "maplestory.market":
        if not first_fetch():
            return
        print("Every history is fetched: building the prices for the first time.", flush=True)
        if run("market_prices.py", "--rebuild", "--web", WEB, "--no-push"):
            sys.exit("the first price build failed")
    else:
        run("fetch_history.py", "--rotate", "--web", WEB)
    if run("market_prices.py", "--live", "--web", WEB, "--live-out", LIVE):
        print("ERROR: the live prices were not updated")
    run("share_image.py", "--site", WEB)
    # Yesterday's last live value becomes its day's point, on the first run after midnight.
    if run("market_prices.py", "--web", WEB, "--live-out", LIVE, "--no-push"):
        print("ERROR: yesterday's point was not saved")


if __name__ == "__main__":
    main()
