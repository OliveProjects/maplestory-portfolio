"""The share image: when the link is shared in Discord, for example, the
account's value and a chart of how it has moved show up.

  python share_image.py [--site <folder>]     (the site's root, the folder the web server shows)

Discord and the like read the page's Open Graph tags without running
scripts. Every hour (run_hourly.sh or actions_job.py, after --live) og.png
is drawn from hourly.json: the account's value, the movement over the last
day and a week's chart, green above the starting value and red below, as on
the page. The tags in the published page get the value and the picture's
time (?v=, otherwise Discord shows an old picture from its cache). Only the
published copy is changed; a deploy resets the repo's tags, which point to
the same picture, until the next hour.

The credit (the API terms, point 2: where a link can't be used, the text is
enough) is both in the picture and in the description.

The font: put Inter (rsms.me/inter, InterVariable.ttf) next to the script
as inter.ttf. Without it the text is drawn with Pillow's built-in font.
"""
import datetime
import json
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

import settings as cfg

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = cfg.TIME_ZONE
FONT = os.path.join(HERE, "inter.ttf")
S = 2                       # drawn at double size and scaled down: smooth lines
W, H = 1200, 630
BG, TEXT, SUB = (10, 10, 10), (240, 240, 240), (170, 170, 170)
GREEN, RED = (102, 187, 106), (229, 57, 53)


def font(size, weight):
    """Inter (variable) if it is next to the script, else Pillow's own."""
    if not os.path.exists(FONT):
        return ImageFont.load_default(size * S)
    f = ImageFont.truetype(FONT, size * S)
    try:
        f.set_variation_by_axes([weight])
    except OSError:
        pass  # not a variable font
    return f


def percent(p):
    return ("+" if p >= 0 else "−") + "%.1f%%" % abs(p)


def draw(points, updated):
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)
    L = 72
    now = points[-1][1]
    day_ago = [p for p in points if p[0] <= points[-1][0] - 86400]
    before = day_ago[-1][1] if day_ago else points[0][1]
    p24 = (now / before - 1) * 100
    colour24 = GREEN if p24 >= 0 else RED

    d.text((L * S, 64 * S), "MAPLESTORY", font=font(22, 600), fill=SUB)
    when = datetime.datetime.fromtimestamp(updated, TZ).strftime("%-d %b %H:%M" if os.name != "nt" else "%#d %b %H:%M")
    d.text(((W - L) * S, 64 * S), when, font=font(22, 500), fill=SUB, anchor="ra")
    big = font(112, 800)
    d.text((L * S, 104 * S), format(now, ","), font=big, fill=TEXT)
    width = d.textlength(format(now, ","), font=big) / S
    d.text(((L + width + 20) * S, 196 * S), "MESOS", font=font(24, 600), fill=SUB, anchor="ls")
    pct = font(40, 700)
    d.text((L * S, 254 * S), percent(p24), font=pct, fill=colour24)
    d.text(((L + d.textlength(percent(p24), font=pct) / S + 16) * S, 254 * S), "past 24 hours",
           font=font(28, 400), fill=SUB)

    # The week's chart, hour by hour.
    week = [p for p in points if p[0] >= points[-1][0] - 7 * 86400]
    x0, x1, y0, y1 = L, W - L, 340, 548
    t0, t1 = week[0][0], week[-1][0]
    lo, hi = min(v for _, v in week), max(v for _, v in week)
    pad = (hi - lo) * 0.08 or hi * 0.01
    lo, hi = lo - pad, hi + pad
    X = lambda t: (x0 + (t - t0) / max(1, t1 - t0) * (x1 - x0)) * S
    Y = lambda v: (y1 - (v - lo) / (hi - lo) * (y1 - y0)) * S
    base = week[0][1]
    yb = Y(base)
    area = Image.new("RGBA", img.size, (0, 0, 0, 0))
    da = ImageDraw.Draw(area)
    # Each segment is split where it crosses the starting value, so that the colour changes exactly there.
    parts = []
    for (ta, va), (tb, vb) in zip(week, week[1:]):
        if (va - base) * (vb - base) < 0:
            tm = ta + (base - va) / (vb - va) * (tb - ta)
            parts += [((ta, va), (tm, base)), ((tm, base), (tb, vb))]
        else:
            parts.append(((ta, va), (tb, vb)))
    for (ta, va), (tb, vb) in parts:
        f = GREEN if (va + vb) / 2 >= base else RED
        da.polygon([(X(ta), yb), (X(ta), Y(va)), (X(tb), Y(vb)), (X(tb), yb)], fill=f + (34,))
    img.paste(area, (0, 0), area)
    d = ImageDraw.Draw(img)
    for x in range(int(x0 * S), int(x1 * S), 14 * S):  # the starting value, dashed
        d.line([(x, yb), (min(x + 6 * S, x1 * S), yb)], fill=(80, 80, 80), width=S)
    for (ta, va), (tb, vb) in parts:
        f = GREEN if (va + vb) / 2 >= base else RED
        d.line([(X(ta), Y(va)), (X(tb), Y(vb))], fill=f, width=4 * S, joint="curve")
    r = 7 * S
    sx, sy = X(t1), Y(week[-1][1])
    d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=GREEN if week[-1][1] >= base else RED, outline=BG, width=3 * S)

    foot = font(22, 500)
    address = cfg.SITE_URL.split("://", 1)[-1].rstrip("/")
    d.text((L * S, 586 * S), address, font=foot, fill=SUB, anchor="ls")
    d.text(((W - L) * S, 586 * S), "Prices from maplestory.market", font=foot, fill=SUB, anchor="rs")
    return img.resize((W, H), Image.LANCZOS), now, p24


def tags(html, now, p24, v):
    title = "MapleStory · %s mesos" % format(now, ",")
    desc = ("%s past 24 hours. The cosmetics in my MapleStory Classic inventory, valued at market "
            "prices. Prices from maplestory.market." % percent(p24))
    for prop, value in (("og:title", title), ("og:description", desc),
                        ("og:image", cfg.SITE_URL + "og.png?v=%d" % v)):
        html = re.sub(r'(<meta property="%s"\s+content=")[^"]*(")' % re.escape(prop),
                      lambda m: m.group(1) + value + m.group(2), html)
    return html


def main(args):
    site = cfg.WEB
    for flag in ("--site", "--sajt"):
        if flag in args:
            site = args[args.index(flag) + 1]
    data = cfg.data_dir(site)
    if not os.path.exists(os.path.join(data, "hourly.json")):
        return  # before the first --rebuild there are no hours
    points = json.load(open(os.path.join(data, "hourly.json"), encoding="utf-8"))["points"]
    lv = os.path.join(data, "live.json")
    updated = json.load(open(lv, encoding="utf-8"))["updated"] if os.path.exists(lv) else points[-1][0]
    img, now, p24 = draw(points, updated)
    img.save(os.path.join(cfg.page_dir(site), "og.png"), optimize=True)
    if "--image-only" in args or "--bara-bild" in args:
        return
    page = os.path.join(cfg.page_dir(site), "index.html")
    html = open(page, encoding="utf-8").read()
    new = tags(html, now, p24, points[-1][0])
    if new != html:
        with open(page, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
    print("share_image: %s mesos, %s" % (format(now, ","), percent(p24)))


if __name__ == "__main__":
    main(sys.argv[1:])
