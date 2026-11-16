"""Delningsbilden: när länken delas i t.ex. Discord syns kontots värde och
en graf över utvecklingen.

  python delning.py [--sajt <mapp>]     (sajtens rot, den mapp webbservern visar)

Discord och liknande läser sidans Open Graph-taggar utan att köra skript.
Varje timme (kor.sh, efter --live) ritas maplestory/og.png ur hourly.json:
kontots värde, utvecklingen senaste dygnet och en veckas graf, grön över
startvärdet och röd under som på sidan. Taggarna i den utlagda sidan får
värdet och bildens tid (?v=, annars visar Discord en gammal bild ur sin
cache). Bara den utlagda kopian ändras; en utläggning återställer repots
taggar, som pekar på samma bild, tills nästa timme.

Krediten (API-villkoren punkt 2: där länk inte går räcker texten) står både
i bilden och i beskrivningen.

Typsnittet: lägg Inter (rsms.me/inter, InterVariable.ttf) bredvid skriptet
som inter.ttf. Utan den ritas texten med Pillows inbyggda typsnitt.
"""
import datetime
import json
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

import installningar as inst

HERE = os.path.dirname(os.path.abspath(__file__))
SVERIGE = inst.TIDSZON
TYPSNITT = os.path.join(HERE, "inter.ttf")
S = 2                       # ritas i dubbel storlek och skalas ned: mjuka linjer
B, H = 1200, 630
BG, TEXT, SUB = (10, 10, 10), (240, 240, 240), (170, 170, 170)
GRON, ROD = (102, 187, 106), (229, 57, 53)


def typsnitt(storlek, vikt):
    """Inter (variabel) om den ligger bredvid skriptet, annars Pillows eget."""
    if not os.path.exists(TYPSNITT):
        return ImageFont.load_default(storlek * S)
    f = ImageFont.truetype(TYPSNITT, storlek * S)
    try:
        f.set_variation_by_axes([vikt])
    except OSError:
        pass  # inte ett variabelt typsnitt
    return f


def procent(p):
    return ("+" if p >= 0 else "−") + "%.1f%%" % abs(p)


def rita(punkter, uppdaterad):
    img = Image.new("RGB", (B * S, H * S), BG)
    d = ImageDraw.Draw(img)
    L = 72
    nu = punkter[-1][1]
    dygn = [p for p in punkter if p[0] <= punkter[-1][0] - 86400]
    fore = dygn[-1][1] if dygn else punkter[0][1]
    p24 = (nu / fore - 1) * 100
    farg24 = GRON if p24 >= 0 else ROD

    d.text((L * S, 64 * S), "MAPLESTORY", font=typsnitt(22, 600), fill=SUB)
    tid = datetime.datetime.fromtimestamp(uppdaterad, SVERIGE).strftime("%-d %b %H:%M" if os.name != "nt" else "%#d %b %H:%M")
    d.text(((B - L) * S, 64 * S), tid, font=typsnitt(22, 500), fill=SUB, anchor="ra")
    stor = typsnitt(112, 800)
    d.text((L * S, 104 * S), format(nu, ","), font=stor, fill=TEXT)
    bredd = d.textlength(format(nu, ","), font=stor) / S
    d.text(((L + bredd + 20) * S, 196 * S), "MESOS", font=typsnitt(24, 600), fill=SUB, anchor="ls")
    pct = typsnitt(40, 700)
    d.text((L * S, 254 * S), procent(p24), font=pct, fill=farg24)
    d.text(((L + d.textlength(procent(p24), font=pct) / S + 16) * S, 254 * S), "past 24 hours",
           font=typsnitt(28, 400), fill=SUB)

    # Veckans graf, timme för timme.
    vecka = [p for p in punkter if p[0] >= punkter[-1][0] - 7 * 86400]
    x0, x1, y0, y1 = L, B - L, 340, 548
    t0, t1 = vecka[0][0], vecka[-1][0]
    lo, hi = min(v for _, v in vecka), max(v for _, v in vecka)
    pad = (hi - lo) * 0.08 or hi * 0.01
    lo, hi = lo - pad, hi + pad
    X = lambda t: (x0 + (t - t0) / max(1, t1 - t0) * (x1 - x0)) * S
    Y = lambda v: (y1 - (v - lo) / (hi - lo) * (y1 - y0)) * S
    bas = vecka[0][1]
    yb = Y(bas)
    yta = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dy = ImageDraw.Draw(yta)
    # Varje segment delas där det korsar startvärdet, så att färgen byter exakt där.
    delar = []
    for (ta, va), (tb, vb) in zip(vecka, vecka[1:]):
        if (va - bas) * (vb - bas) < 0:
            tm = ta + (bas - va) / (vb - va) * (tb - ta)
            delar += [((ta, va), (tm, bas)), ((tm, bas), (tb, vb))]
        else:
            delar.append(((ta, va), (tb, vb)))
    for (ta, va), (tb, vb) in delar:
        f = GRON if (va + vb) / 2 >= bas else ROD
        dy.polygon([(X(ta), yb), (X(ta), Y(va)), (X(tb), Y(vb)), (X(tb), yb)], fill=f + (34,))
    img.paste(yta, (0, 0), yta)
    d = ImageDraw.Draw(img)
    for x in range(int(x0 * S), int(x1 * S), 14 * S):  # startvärdet, streckat
        d.line([(x, yb), (min(x + 6 * S, x1 * S), yb)], fill=(80, 80, 80), width=S)
    for (ta, va), (tb, vb) in delar:
        f = GRON if (va + vb) / 2 >= bas else ROD
        d.line([(X(ta), Y(va)), (X(tb), Y(vb))], fill=f, width=4 * S, joint="curve")
    r = 7 * S
    sx, sy = X(t1), Y(vecka[-1][1])
    d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=GRON if vecka[-1][1] >= bas else ROD, outline=BG, width=3 * S)

    fot = typsnitt(22, 500)
    adress = inst.SAJT_URL.split("://", 1)[-1].rstrip("/")
    d.text((L * S, 586 * S), adress, font=fot, fill=SUB, anchor="ls")
    d.text(((B - L) * S, 586 * S), "Prices from maplestory.market", font=fot, fill=SUB, anchor="rs")
    return img.resize((B, H), Image.LANCZOS), nu, p24


def taggar(html, nu, p24, v):
    titel = "MapleStory · %s mesos" % format(nu, ",")
    besk = ("%s past 24 hours. The cosmetics in my MapleStory Classic inventory, valued at market "
            "prices. Prices from maplestory.market." % procent(p24))
    for prop, varde in (("og:title", titel), ("og:description", besk),
                        ("og:image", inst.SAJT_URL + "og.png?v=%d" % v)):
        html = re.sub(r'(<meta property="%s"\s+content=")[^"]*(")' % re.escape(prop),
                      lambda m: m.group(1) + varde + m.group(2), html)
    return html


def main(args):
    global SAJT
    SAJT = args[args.index("--sajt") + 1] if "--sajt" in args else inst.WEB
    data = inst.data(SAJT)
    if not os.path.exists(os.path.join(data, "hourly.json")):
        return  # före första --bakat finns inga timmar
    punkter = json.load(open(os.path.join(data, "hourly.json"), encoding="utf-8"))["points"]
    lv = os.path.join(data, "live.json")
    uppdaterad = json.load(open(lv, encoding="utf-8"))["updated"] if os.path.exists(lv) else punkter[-1][0]
    img, nu, p24 = rita(punkter, uppdaterad)
    img.save(os.path.join(inst.sida(SAJT), "og.png"), optimize=True)
    if "--bara-bild" in args:
        return
    sida = os.path.join(inst.sida(SAJT), "index.html")
    html = open(sida, encoding="utf-8").read()
    ny = taggar(html, nu, p24, punkter[-1][0])
    if ny != html:
        with open(sida, "w", encoding="utf-8", newline="\n") as f:
            f.write(ny)
    print("delning: %s mesos, %s" % (format(nu, ","), procent(p24)))


if __name__ == "__main__":
    main(sys.argv[1:])
