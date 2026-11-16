"""The hourly price job when it runs on GitHub Actions (the workflow that
website_creator.py adds to your site repo) instead of on your own server.

It lives in .portfolio/ in the site repo, next to the scripts it calls, and
does what kor.sh does on a server, with one difference: Actions cannot wait
for hours, so the first fetch of every item's history is spread over the
hourly runs, five calls per hour, and the first price build (--bakat) runs
by itself once everything is fetched.

The API key comes from the repo secret MAPLESTORY_MARKET_KEY, written to
nyckel.txt by the workflow and deleted after the run. The fetched history
(historik/, agare/, dagssvar/, timmar.json) is kept in the Actions cache and
never committed: maplestory.market's terms do not allow republishing their
data in bulk.

    python actions_steg.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import hamta_historik as hh  # noqa: E402
import installningar as inst  # noqa: E402

WEB = os.path.abspath(os.path.join(HERE, ".."))
DATA = inst.data(WEB)
PRICES = os.path.join(DATA, "prices.json")
LIVE = os.path.join(DATA, "live.json")


def kor(*args):
    print("> python " + " ".join(args), flush=True)
    return subprocess.run([sys.executable] + list(args)).returncode


def forsta_hamtningen():
    """Fetches up to five missing histories. True when nothing is missing."""
    os.makedirs(hh.UT, exist_ok=True)

    def saknas():
        return [(s, f) for s, f in hh.EXTRA if not os.path.exists(f)] + [
            ("items/%s/history" % i, os.path.join(hh.UT, i + ".json")) for i in hh.alla_id(PRICES)
            if not os.path.exists(os.path.join(hh.UT, i + ".json"))]

    kvar = saknas()
    print("First fetch: %d calls left (5 per hour)" % len(kvar), flush=True)
    for sokvag, fil in kvar[:hh.PER_TIMME]:
        data, h = hh.hamta(sokvag)
        if data is None:
            print("429: this hour's calls are used up", flush=True)
            break
        hh.spara(fil, data)
        print("  " + sokvag, flush=True)
        if int(h.get("x-ratelimit-remaining", 1)) <= 0:
            break
    return not saknas()


def main():
    if not os.path.exists(PRICES):
        print("No prices.json yet: run the first sync on your computer (Synka.bat).")
        return
    kalla = json.load(open(PRICES, encoding="utf-8")).get("source")
    if kalla != "maplestory.market":
        if not forsta_hamtningen():
            return
        print("Every history is fetched: building the prices for the first time.", flush=True)
        if kor("marknadspriser.py", "--bakat", "--web", WEB, "--ingen-push"):
            sys.exit("the first price build failed")
    else:
        kor("hamta_historik.py", "--rotera", "--web", WEB)
    if kor("marknadspriser.py", "--live", "--web", WEB, "--live-ut", LIVE):
        print("ERROR: the live prices were not updated")
    kor("delning.py", "--sajt", WEB)
    # Yesterday's last live value becomes today's point, on the first run after midnight.
    if kor("marknadspriser.py", "--web", WEB, "--live-ut", LIVE, "--ingen-push"):
        print("ERROR: yesterday's point was not saved")


if __name__ == "__main__":
    main()
