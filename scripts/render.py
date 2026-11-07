#!/usr/bin/env python3
"""Render Persian text as a Nastaliq or Shekasteh-Nastaliq manuscript sheet (SVG).

The letters are the font's own glyph outlines. Words are spaced by their ink, crowded
dots and marks are moved clear of other letters, Nastaliq misra' get a keshideh and are
matched in length, and Shekasteh lines rise at the end.

    python scripts/render.py --style nastaliq "misra 1" "misra 2" --author "حافظ" -o out.svg
    python scripts/render.py --style shekasteh "one line" --png out.png -o out.svg

See SKILL.md for the rules this implements.
"""
import argparse
import math
import re
import signal
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image, ImageDraw, ImageFont, features as pil_features
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen, DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.basePen import BasePen
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely import affinity

ROOT = Path(__file__).resolve().parent.parent
FONTS = {
    'nastaliq': ROOT / 'fonts' / 'IranNastaliq.ttf',
    'shekasteh': ROOT / 'fonts' / 'DimaShekastehFree.ttf',
}
PX = {'nastaliq': 56, 'shekasteh': 64}
RISE = {'flat': (0, 0), 'gentle': (24, .45), 'medium': (40, .40), 'strong': (56, .35), 'stronger': (72, .30)}

TATWEEL, ZWNJ = 'ـ', '‌'
NON_JOINERS = set('اآأإدذرزژوؤ')           # never connect to the next letter
PARTICLES = {'به', 'که', 'را', 'و', 'یا', 'چه', 'از', 'در', 'با', 'تا', 'بر', 'هر', 'نه', 'گر', 'اگر', 'چو'}
DOTS = dict(zip('بپتثجچخذزژشضظغفقنیيةک', [1, 3, 2, 3, 1, 3, 1, 1, 1, 3, 3, 1, 1, 1, 1, 2, 1, 2, 2, 2, 0]))
MARKED = set('آأإؤئۀ')                     # letters that carry a madda or hamza
SAD_FAMILY = set('صض')                      # stretch cleanly into any run end (اعضـای)
STRETCHABLE = set('بپتثنیئفقکگ')            # stretchable only into a word's final letter
HARAKAT = set(chr(c) for c in range(0x064B, 0x0660)) | {'ٰ'}

W, H, BY = 5000, 800, 480
# --mashq: climb per px, word-gap factor, tuck, line clearance (page px), dot clearance (dot heights)
MASHQ = dict(slope=0.24, gap=0.6, tuck=0.6, lead=14, room=0.9)
TASHKIL_SCALE = 0.8                         # harakat in a finer pen than the letters                   # mask canvas (px) and its baseline

COLORS = dict(paper='#f3e8d3', ink='#5a1a1f', name='#6e2a2a',
              dpaper='#241a16', dink='#ecd3b2', dname='#d9b48a')


# ---------------------------------------------------------------- raster masks (spacing)

class Masker:
    """Renders words with Pillow/RAQM as boolean masks, cached at x=0 and shifted."""

    def __init__(self, font_path, px, feats):
        if not pil_features.check('raqm'):
            sys.exit('Pillow was built without libraqm; Persian shaping needs it.')
        self.pil = ImageFont.truetype(str(font_path), px, layout_engine=ImageFont.Layout.RAQM)
        self.feats = feats

    @lru_cache(maxsize=4096)
    def base(self, s, dy):
        m = Image.new('L', (W, H), 0)
        ImageDraw.Draw(m).text((0, BY + dy), s, font=self.pil, fill=255, anchor='ls',
                               direction='rtl', language='fa', features=self.feats or None)
        return np.array(m) > 100

    def __call__(self, s, x=0, dy=0):
        b = self.base(s, dy)
        if x == 0:
            return b
        out = np.zeros_like(b)
        if x > 0:
            out[:, x:] = b[:, :W - x]
        else:
            out[:, :W + x] = b[:, -x:]
        return out

    def width(self, s):
        cols = np.where(self.base(s, 0).any(0))[0]
        return int(cols[-1] - cols[0]) if len(cols) else 0


def lifts(mask, words, rise, start, slope=0):
    if slope:   # mashq: a steady diagonal climb from the first word to the last
        wid = [max(1, mask.width(w)) for w in words]
        return [-round(slope * (sum(wid[:k]) + (wd - wid[0]) / 2)) for k, wd in enumerate(wid)]
    if not rise:
        return [0] * len(words)
    wid = [max(1, mask.width(w)) for w in words]
    tot, run, out = sum(wid), 0, []
    for wd in wid:
        t = (run + wd / 2) / tot
        run += wd
        out.append(-round(rise * max(0, (t - start) / (1 - start)) ** 2))
    return out


def pack_line(mask, text, clear, rise=0, start=0.35, maxtuck=0.35, slope=0):
    """Place words right to left, each sliding in until its ink is `clear` px from the
    text already placed. Returns [(word, x_px, dy_px)]; dy negative = up."""
    words = text.split(' ')
    out, acc = [], None
    for w, dy in zip(words, lifts(mask, words, rise, start, slope)):
        m = mask(w, 0, dy)
        cols = np.where(m.any(0))[0]
        wx0, wx1 = cols[0], cols[-1]
        wd = wx1 - wx0
        if acc is None:
            x = W - 200 - wx1
        else:
            left = np.full(H, W, float)
            ys, xs = np.nonzero(acc)
            np.minimum.at(left, ys, xs)
            left = sliding_window_view(np.pad(left, clear, constant_values=W), 2 * clear + 1).min(1)
            right = np.full(H, -1.0)
            ys, xs = np.nonzero(m)
            np.maximum.at(right, ys, xs)
            ok = right >= 0
            shift = (left[ok] - right[ok] - clear).min()
            x = int(min(shift, np.where(acc.any(0))[0][0] - maxtuck * wd - wx0))
        placed = mask(w, x, dy)
        acc = placed if acc is None else (acc | placed)
        out.append((w, x, dy))
    return out


def ink_width(mask, placed):
    acc = None
    for w, x, dy in placed:
        m = mask(w, x, dy)
        acc = m if acc is None else acc | m
    return int(np.ptp(np.where(acc.any(0))[0]))


def base_gap(text, px):
    """0.2 em, stepping up to 0.25 em for lines of many non-joiner endings or 1-letter words."""
    words = [w for w in text.split(' ') if w]
    ends = sum(1 for w in words if w.rstrip(''.join(HARAKAT))[-1:] in NON_JOINERS)
    one = any(len(w.strip(''.join(HARAKAT))) == 1 for w in words)
    return round((0.25 if (ends >= 3 or one) else 0.2) * px)


# ---------------------------------------------------------------- keshideh (Nastaliq)

def runs_of(word):
    """Connected runs of a word as lists of (index, letter). A run ends at a non-joiner
    (which still receives the join from the letter before it) or at a ZWNJ."""
    runs, run = [], []
    for i, c in enumerate(word):
        if c in HARAKAT:
            continue
        if c == ZWNJ:
            if run:
                runs.append(run)
            run = []
            continue
        run.append((i, c))
        if c in NON_JOINERS:
            runs.append(run); run = []
    if run:
        runs.append(run)
    return runs


def joints(word):
    """Classical keshideh places in a word, best first: [(tier, index of the letter to stretch)].

    A keshideh stretches the second-to-last letter of a connected run into the run's last
    letter, never the word's first letter.
      tier 1: ص ض at the end of any run: اعضـای.
      tier 2: ب پ ت ث ن ی ئ ف ق ک گ into the word's final letter only: الرحیـم، مشکـل‌ها.
    Never س or ش: in Nastaliq the keshideh itself stands for the sin, but IranNastaliq draws
    the teeth and then the stretch, which reads as sin + keshideh (کأسـا، بسـم).
    ل, ه, the ح family, م and ع draw badly stretched (الـرحیم reads as السرحیم)."""
    if word in PARTICLES or TATWEEL in word:
        return []
    runs = runs_of(word)
    if sum(len(r) for r in runs) < 3:
        return []
    first = runs[0][0][0]
    out = []
    for k in range(len(runs) - 1, -1, -1):
        r = runs[k]
        if len(r) < 2 or r[-2][0] == first:
            continue
        i, c = r[-2]
        last = k == len(runs) - 1 or word[r[-1][0] + 1:r[-1][0] + 2] == ZWNJ
        tier = 1 if c in SAD_FAMILY else (2 if c in STRETCHABLE and last else 0)
        if tier:
            while i + 1 < len(word) and word[i + 1] in HARAKAT:   # after the letter's marks
                i += 1
            out.append((tier, i))
    return out


def keshideh_options(mask, text):
    """[(line_with_tatweel, word_index, gain)]: tier 1 places first, then tier 2; within a
    tier, from the last content word back."""
    words = text.split(' ')
    opts = []
    for wi in range(len(words) - 1, -1, -1):
        w = words[wi]
        w0 = mask.width(w)
        for tier, i in joints(w):
            cand = w[:i + 1] + TATWEEL + w[i + 1:]
            gain = mask.width(cand) / max(1, w0)
            if gain >= 1.3:
                opts.append((tier, len(words) - wi, ' '.join(words[:wi] + [cand] + words[wi + 1:]), wi, gain))
    return [(c, wi, g) for _, _, c, wi, g in sorted(opts, key=lambda o: (o[0], o[1]))]


def equalize(mask, lines, px, report, auto_keshideh=True):
    """Give the shorter misra' a keshideh on its last content word (if auto_keshideh), then
    widen its word gap until both ink widths match (within ~1%)."""
    gaps = [base_gap(t, px) for t in lines]
    widths = [ink_width(mask, pack_line(mask, t, g)) for t, g in zip(lines, gaps)]
    short = 0 if widths[0] < widths[1] else 1
    long_ = 1 - short
    target = widths[long_]
    lines = list(lines)
    chosen = None
    for cand, wi, gain in (keshideh_options(mask, lines[short]) if auto_keshideh else []):
        wdt = ink_width(mask, pack_line(mask, cand, gaps[short]))
        if wdt <= target * 1.01:
            chosen = (cand, wi, gain, wdt)
            break
    if not auto_keshideh:
        pass
    elif chosen is None:
        report.append("keshideh: no classical joint fits the shorter misra'; plain gaps only")
    else:
        lines[short] = chosen[0]
        report.append(f"keshideh: misra' {short + 1}, word '{chosen[0].split(' ')[chosen[1]]}' (+{(chosen[2] - 1) * 100:.0f}%)")
    best = None
    for g in range(gaps[short], round(0.45 * px) + 1):
        wdt = ink_width(mask, pack_line(mask, lines[short], g))
        d = abs(wdt - target)
        if best is None or d < best[0]:
            best = (d, g)
        if wdt >= target:
            break
    gaps[short] = best[1]
    report.append(f"gaps: {gaps[0]}px / {gaps[1]}px, width diff {best[0] / target * 100:.1f}%")
    return lines, gaps


# ---------------------------------------------------------------- outlines, dots, marks

class FlatPen(BasePen):
    """Flatten a contour's curves into points (geometry checks only)."""

    def __init__(self, steps=8):
        super().__init__(None)
        self.polys, self.cur, self.n = [], [], steps

    def _moveTo(self, p): self.cur = [p]
    def _lineTo(self, p): self.cur.append(p)

    def _curveToOne(self, a, b, c):
        p = self.cur[-1]
        for k in range(1, self.n + 1):
            t = k / self.n; u = 1 - t
            self.cur.append((u**3*p[0] + 3*u*u*t*a[0] + 3*u*t*t*b[0] + t**3*c[0],
                             u**3*p[1] + 3*u*u*t*a[1] + 3*u*t*t*b[1] + t**3*c[1]))

    def _qCurveToOne(self, a, b):
        p = self.cur[-1]
        for k in range(1, self.n + 1):
            t = k / self.n; u = 1 - t
            self.cur.append((u*u*p[0] + 2*u*t*a[0] + t*t*b[0], u*u*p[1] + 2*u*t*a[1] + t*t*b[1]))

    def _closePath(self):
        if len(self.cur) > 2:
            self.polys.append(self.cur)
        self.cur = []
    _endPath = _closePath


class Outliner:
    def __init__(self, font_path, feats):
        self.hf = hb.Font(hb.Face(hb.Blob.from_file_path(str(font_path))))
        self.tt = TTFont(str(font_path))
        self.gs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.upem = self.tt['head'].unitsPerEm
        self.feats = {f: True for f in feats}
        gdef = self.tt['GDEF'].table.GlyphClassDef.classDefs if 'GDEF' in self.tt else {}
        self.marks = {g for g, c in gdef.items() if c == 3}

    def contours(self, placed, px):
        """Every contour of a packed line in font units (y up), components decomposed."""
        items, g = [], 0
        for word, xpx, dypx in placed:
            buf = hb.Buffer()
            buf.add_codepoints([ord(c) for c in word])
            buf.guess_segment_properties()
            buf.language = 'fa'
            hb.shape(self.hf, buf, self.feats)
            x = xpx * self.upem / px
            y0 = -dypx * self.upem / px
            starts = sorted({i.cluster for i in buf.glyph_infos}) + [len(word)]
            span = {c: word[c:starts[k + 1]] for k, c in enumerate(starts[:-1])}
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
                rec = DecomposingRecordingPen(self.gs)
                gname = self.order[info.codepoint]
                self.gs[gname].draw(
                    TransformPen(rec, (1, 0, 0, 1, x + pos.x_offset, y0 + pos.y_offset)))
                cur = []
                for op, args in rec.value:
                    cur.append((op, args))
                    if op in ('closePath', 'endPath'):
                        r = RecordingPen(); r.value = cur; fp = FlatPen(); r.replay(fp); cur = []
                        if fp.polys:
                            items.append(dict(cmds=r.value, poly=Polygon(fp.polys[0]).buffer(0), g=g,
                                              ch=word[info.cluster], chars=span[info.cluster], word=word,
                                              mark=gname in self.marks, lig=gname.lower().startswith('lellah'),
                                              dx=0.0, dy=0.0, sc=1.0, ox=0.0, oy=0.0))
                g += 1
                x += pos.x_advance
        for it in items:   # a contour inside a bigger contour of the same glyph is a counter
            it['hole'] = any(o is not it and o['g'] == it['g'] and o['poly'].area > it['poly'].area
                             and o['poly'].contains(it['poly']) for o in items)
        # tashkil: every contour of a mark glyph, plus the shadda and small alif that the
        # الله ligature draws inside itself (all its pieces except the body)
        for it in items:
            if it['lig'] and not it['hole']:
                body = max((o for o in items if o['g'] == it['g']), key=lambda o: o['poly'].area)
                it['mark'] = it is not body
        return items

    def reference_dot(self, sample="بنیت تبنی نبتی یبنت بیت نیب"):
        """(area, width, height) of one dot: the most common free contour in dotted letters."""
        items = self.contours([(w, 0, 0) for w in sample.split()], 100)
        ink = [i for i in items if not i['hole']]
        fl = []
        for it in ink:
            own = [o['poly'] for o in ink if o['g'] == it['g'] and o is not it]
            b = it['poly'].bounds; w = b[2] - b[0]; h = b[3] - b[1]
            if own and all(p.distance(it['poly']) > 0 for p in own) and 1.0 <= h / w <= 1.6:
                fl.append((it['poly'].area, w, h))
        if not fl:   # dots are not separate contours in this font: fall back to all small ones
            fl = [(i['poly'].area, i['poly'].bounds[2] - i['poly'].bounds[0],
                   i['poly'].bounds[3] - i['poly'].bounds[1]) for i in ink]
        best = max(fl, key=lambda f: sum(abs(g[0] - f[0]) <= 0.08 * f[0] for g in fl))
        cl = sorted((g for g in fl if abs(g[0] - best[0]) <= 0.08 * best[0]), key=lambda g: g[0])
        return cl[len(cl) // 2]


def is_dot_group(poly, A, Wd, Hd):
    a = poly.area; k = round(a / A)
    if k < 1 or k > 3 or abs(a - k * A) > 0.20 * k * A:
        return False
    b = poly.bounds; w = b[2] - b[0]; h = b[3] - b[1]
    if k == 1 and (h > 1.3 * Hd or w > 1.3 * Wd):
        return False
    return w <= 2.4 * Wd and h <= 2.4 * Hd and poly.area / poly.convex_hull.area > 0.8


def has_counter(it, items):
    return any(h['hole'] and h['g'] == it['g'] and it['poly'].contains(h['poly'].representative_point())
               for h in items)


# ---------------------------------------------------------------- page-level dot pass
#
# Dots and marks are settled once, after the whole page is laid out: every dot and mark is
# checked against all ink on the page (its neighbours, other words, the other misra'), and
# only the ones that actually crowd something are moved, by the smallest move that clears
# everything, with already-moved dots counted as ink. Passes repeat until nothing changes.

def movable(it, glyph, A, Wd, Hd):
    """'dot', 'mark' or None for one contour. glyph = the other contours of its glyph."""
    if it['mark']:
        return None                     # tashkil are settled as their own units
    if it['hole'] or not glyph or any(p.distance(it['poly']) == 0 for p in glyph):
        return None                     # touches its own letter: part of the letter
    if is_dot_group(it['poly'], A, Wd, Hd):
        # no more dots than the letter has: IranNastaliq's solid ف head is two dots in
        # size, but ف has one dot, so it is the head (moving it lifts ف off the ر).
        # The ی dot pair is one contour, larger than the ی tooth, and stays movable.
        return 'dot' if round(it['poly'].area / A) <= sum(DOTS.get(c, 0) for c in it['chars']) else None
    if it['ch'] in MARKED and it['poly'].area <= 4 * A:
        body = max(glyph, key=lambda p: p.area)
        if it['poly'].area > 0.5 * body.area:     # IranNastaliq's alef of أ is its own contour
            return None
        mb, ob = it['poly'].bounds, unary_union(glyph).bounds
        above, below = mb[1] > ob[3], mb[3] < ob[1]
        if (below and it['ch'] == 'إ') or (above and it['ch'] != 'إ'):
            return 'mark'
    return None


def home(p, orig, ownu, Wd, Hd, slack=0.5):
    """A moved dot or mark still reads as its own letter's: horizontally over that letter,
    and no further from it than it started. (A ن dot pushed over the next د would make
    گوهرند read «گوهرنز».)"""
    x0, _, x1, _ = ownu.bounds
    return x0 - 0.3 * Wd <= p.centroid.x <= x1 + 0.3 * Wd and \
        ownu.distance(p) <= ownu.distance(orig) + slack * Hd


def settle(page, A, Wd, Hd, passes=4, margin=1.4, room=0.5):
    """page: list of contour dicts in one coordinate space, each with a page-unique 'g'.
    Moves crowded dots and marks in place (updating 'poly', 'dx', 'dy').

    A letter's dots move together as one group, keeping their arrangement (Dima draws each
    dot of ش or ی as its own piece; moving them one by one can merge them into a blob).
    Returns (moved, unresolved) as lists of (kind, letter, word)."""
    clear = room * Hd; target = clear * margin
    ink = [i for i in page if not i['hole']]
    byg = {}
    for i in ink:
        byg.setdefault(i['g'], []).append(i)
    units = {}                                   # (glyph, kind) -> contours moved together
    for it in ink:
        if it['mark']:
            units.setdefault((it['g'], 'tashkil'), []).append((it, 'tashkil'))
            continue
        glyph = [o['poly'] for o in byg[it['g']] if o is not it]
        kind = movable(it, glyph, A, Wd, Hd)
        if kind:
            units.setdefault((it['g'], kind if kind == 'dot' else id(it)), []).append((it, kind))
    cands = []
    for key, members in units.items():
        parts = [m for m, _ in members]
        body = [o['poly'] for o in byg[key[0]] if all(o is not m for m in parts)]
        if body:
            cands.append((parts, members[0][1], unary_union(body)))
        elif members[0][1] == 'tashkil':   # a mark glyph: no body of its own
            cands.append((parts, 'tashkil', None))
    union = lambda parts: unary_union([m['poly'] for m in parts])
    solid = lambda o: o['poly'].area >= 0.5 * A or o['mark']     # ink a dot must clear (Dima's ب dot is 0.74 A)
    tclear = 0.4 * Hd                                            # tashkil to any other ink
    moved = {}
    for _ in range(passes):
        changed = False
        for parts, kind, ownu in cands:
            g = parts[0]['g']
            cur = union(parts)
            if kind == 'tashkil':
                # clear of all other ink, its own letter included (it is a separate glyph,
                # or, in الله, a separate piece the font happens to draw close)
                others = unary_union([o['poly'] for o in ink if all(o is not m for m in parts)
                                      and (o['poly'].area >= 0.15 * A or o['mark'])])
                if others.is_empty or others.distance(cur) >= tclear:
                    continue
                from shapely.ops import nearest_points
                pa, pb = nearest_points(others, cur)
                base = math.degrees(math.atan2(pb.y - pa.y, pb.x - pa.x)) if pa.distance(pb) > 1e-6 \
                    else (90 if cur.centroid.y > others.centroid.y else -90)
                best = None
                # nearest-free direction first, then the rest of the circle (a kasra under a
                # final م must step past the م's long tail, sideways)
                for da in [0, 15, -15, 30, -30, 45, -45, 60, -60, 75, -75, 90, -90,
                           105, -105, 120, -120, 135, -135, 150, -150, 165, -165, 180]:
                    a = math.radians(base + da); ux, uy = math.cos(a), math.sin(a)
                    r = 10
                    while r <= 2.5 * Hd and (best is None or r < best[0]):
                        if abs(ux * r) <= 1.5 * Wd:
                            q = affinity.translate(cur, ux * r, uy * r)
                            if others.distance(q) >= tclear * margin:
                                best = (r, ux * r, uy * r)
                                break
                        r += 10
                if best is not None:
                    for m in parts:
                        m['dx'] += best[1]; m['dy'] += best[2]
                        m['poly'] = affinity.translate(m['poly'], best[1], best[2])
                    moved[id(parts[0])] = (kind, ''.join(c for c in parts[0]['chars'] if c in HARAKAT) or parts[0]['ch'],
                                           parts[0]['word']); changed = True
                continue
            others = unary_union([o['poly'] for o in ink if o['g'] != g and solid(o)])
            if others.is_empty:
                continue
            if others.distance(cur) >= clear:
                continue
            best = None
            if kind == 'dot':
                below = cur.centroid.y < ownu.centroid.y; sg = -1 if below else 1
                for a in list(range(-90, 91, 15)) + list(range(105, 271, 15)):   # away from its letter first
                    ux, uy = math.sin(math.radians(a)), sg * math.cos(math.radians(a))
                    r = 10
                    while r <= 1.3 * Hd and (best is None or r < best[0]):
                        q = affinity.translate(cur, ux * r, uy * r)
                        if others.distance(q) >= target and home(q, cur, ownu, Wd, Hd):
                            best = (r, ux * r, uy * r)
                            break
                        r += 10
            else:
                cx, ocx = cur.centroid.x, ownu.centroid.x; R = 3 * Hd; n = int(R // 20)
                for i in range(-n, n + 1):
                    for j in range(-n, n + 1):
                        dx, dy = i * 20, j * 20; r = math.hypot(dx, dy)
                        cost = r + 0.5 * abs(cx + dx - ocx)
                        if r > R or (best and cost >= best[0]):
                            continue
                        q = affinity.translate(cur, dx, dy)
                        if others.distance(q) >= target and ownu.distance(q) >= 0.3 * Hd \
                                and home(q, cur, ownu, Wd, Hd):
                            best = (cost, dx, dy)
            if best is None:
                continue
            for m in parts:
                m['dx'] += best[1]; m['dy'] += best[2]
                m['poly'] = affinity.translate(m['poly'], best[1], best[2])
            moved[id(parts[0])] = (kind, parts[0]['ch'], parts[0]['word']); changed = True
        if not changed:
            break
    # last resort, only for dots that still sit on ink (Dima draws the ت dots of نیستان on
    # the sweep of its final ن): hop over the stroke, up to 2.5 dot heights, still over
    # the dot's own letter
    for parts, kind, ownu in cands:
        if kind != 'dot':
            continue
        g = parts[0]['g']; cur = union(parts)
        others = unary_union([o['poly'] for o in ink if o['g'] != g and solid(o)])
        if others.is_empty or others.distance(cur) >= 0.15 * Hd:
            continue
        best = None
        for a in range(0, 360, 15):
            ux, uy = math.sin(math.radians(a)), math.cos(math.radians(a))
            r = 10
            while r <= 2.5 * Hd and (best is None or r < best[0]):
                q = affinity.translate(cur, ux * r, uy * r)
                if others.distance(q) >= clear and ownu.distance(q) >= 0.2 * Hd \
                        and home(q, cur, ownu, Wd, Hd, slack=2.0):
                    best = (r, ux * r, uy * r)
                    break
                r += 10
        if best:
            for m in parts:
                m['dx'] += best[1]; m['dy'] += best[2]
                m['poly'] = affinity.translate(m['poly'], best[1], best[2])
            moved[id(parts[0])] = (kind, parts[0]['ch'], parts[0]['word'])
    unresolved = []
    for parts, kind, ownu in cands:
        g = parts[0]['g']
        if kind == 'tashkil':
            others = unary_union([o['poly'] for o in ink if all(o is not m for m in parts)
                                  and (o['poly'].area >= 0.15 * A or o['mark'])])
            lim = tclear
        else:
            others = unary_union([o['poly'] for o in ink if o['g'] != g and solid(o)]); lim = clear
        if not others.is_empty and others.distance(union(parts)) < lim:
            unresolved.append((kind, parts[0]['ch'], parts[0]['word']))
    return list(moved.values()), unresolved


def spread_dots(page, A, Wd, Hd, cap=1.6, gain=0.25):
    """Mashq only: after the collision pass, let each letter's dots drift into the open
    space around them, as a calligrapher spaces dots to balance the white of the page.
    A dot group drifts mostly vertically (at most a quarter dot width sideways, so it never
    slides onto the next letter: شکایت must not read «سکاثت»), stays on the same side of
    its letter, may move at most 1.5 dot heights further out, and moves only when that clears
    it from other ink by a real margin. Returns [(kind, letter, word)]."""
    ink = [i for i in page if not i['hole']]
    byg = {}
    for i in ink:
        byg.setdefault(i['g'], []).append(i)
    units = {}
    for it in ink:
        if it['mark']:
            continue
        glyph = [o['poly'] for o in byg[it['g']] if o is not it]
        if movable(it, glyph, A, Wd, Hd) == 'dot':
            units.setdefault(it['g'], []).append(it)
    moved = []
    for g, parts in units.items():
        body = [o['poly'] for o in byg[g] if all(o is not m for m in parts)]
        if not body:
            continue
        ownu = unary_union(body)
        cur = unary_union([m['poly'] for m in parts])
        bx = cur.bounds; R = 3 * Hd
        near = [o['poly'] for o in ink if o['g'] != g and (o['poly'].area >= 0.8 * A or o['mark'])
                and o['poly'].bounds[2] > bx[0] - R and o['poly'].bounds[0] < bx[2] + R
                and o['poly'].bounds[3] > bx[1] - R and o['poly'].bounds[1] < bx[3] + R]
        others = unary_union(near) if near else None
        clear = lambda q: min(others.distance(q) if others is not None else cap * Hd, cap * Hd)
        up = cur.centroid.y >= ownu.centroid.y
        d0 = ownu.distance(cur)
        x0, _, x1, _ = ownu.bounds
        base = clear(cur); best = (base, 0, 0)
        step = Hd / 4
        for i in range(-2, 3):
            for j in range(0, 7):
                dx, dy = i * Wd / 8, (j if up else -j) * step
                if not (dx or dy):
                    continue
                q = affinity.translate(cur, dx, dy)
                if not (x0 - 0.5 * Wd <= q.centroid.x <= x1 + 0.5 * Wd):
                    continue
                dd = ownu.distance(q)
                if dd > d0 + 1.5 * Hd or dd < min(d0, 0.3 * Hd):
                    continue
                sc = clear(q) - 0.15 * math.hypot(dx, dy)
                if sc > best[0]:
                    best = (sc, dx, dy)
        if best[0] - base < gain * Hd:
            continue
        for m in parts:
            m['dx'] += best[1]; m['dy'] += best[2]
            m['poly'] = affinity.translate(m['poly'], best[1], best[2])
        moved.append(('dot', parts[0]['ch'], parts[0]['word']))
    # a dot squeezed onto its own letter (the ز dot of زن, fitted between the ز and the ن)
    # is lifted clear of it, onto the height of the neighbouring dots, so it lines up
    # with them as a calligrapher's dots do, instead of hugging the stroke
    cy = {g: unary_union([m['poly'] for m in ps]).centroid for g, ps in units.items()}
    for g, parts in units.items():
        body = [o['poly'] for o in byg[g] if all(o is not m for m in parts)]
        if not body:
            continue
        ownu = unary_union(body)
        cur = unary_union([m['poly'] for m in parts])
        if ownu.distance(cur) >= 0.35 * Hd:
            continue
        up = cur.centroid.y >= ownu.centroid.y; sg = 1 if up else -1
        others = unary_union([o['poly'] for o in ink if o['g'] != g and (o['poly'].area >= 0.5 * A or o['mark'])])
        x0, _, x1, _ = ownu.bounds
        peers = [c.y for h, c in cy.items() if h != g and abs(c.x - cur.centroid.x) <= 4 * Wd
                 and (c.y - cur.centroid.y) * sg > 0]
        best = None
        # anywhere over the letter, hopping a crossing stroke if need be (up to 6 dot
        # heights); kept 2 dot widths from any other letter's dots so it does not pair
        # with them (ز's dot beside ن's would read as ت)
        odots = [unary_union([m['poly'] for m in ps]) for h, ps in units.items() if h != g]
        for i in range(-12, 5):
            for j in range(1, 25):
                dx, dy = i * Wd / 4, sg * j * Hd / 4
                q = affinity.translate(cur, dx, dy)
                if not (x0 - 0.5 * Wd <= q.centroid.x <= x1 + 0.5 * Wd):
                    continue
                if ownu.distance(q) < 0.6 * Hd or others.distance(q) < 0.9 * Hd \
                        or any(d.distance(q) < 2.0 * Wd for d in odots):
                    continue
                align = min((abs(q.centroid.y - y) for y in peers), default=0)
                cost = align + 0.3 * math.hypot(dx, dy)
                if best is None or cost < best[0]:
                    best = (cost, dx, dy)
        if best is None:
            continue
        for m in parts:
            m['dx'] += best[1]; m['dy'] += best[2]
            m['poly'] = affinity.translate(m['poly'], best[1], best[2])
        cy[g] = unary_union([m['poly'] for m in parts]).centroid
        moved.append(('dot', parts[0]['ch'], parts[0]['word']))
    return moved


def shrink_tashkil(items, scale):
    """Draw tashkil a little smaller than the font does, each mark unit (one mark glyph,
    e.g. a shadda with its vowel) about its own centre: calligraphers write harakat with
    a finer pen than the letters."""
    if scale == 1:
        return
    units = {}
    for it in items:
        if it['mark']:
            units.setdefault(it['g'], []).append(it)
    for parts in units.values():
        c = unary_union([m['poly'] for m in parts]).centroid
        for m in parts:
            m['sc'], m['ox'], m['oy'] = scale, c.x, c.y
            m['poly'] = affinity.scale(m['poly'], scale, scale, origin=(c.x, c.y))


def line_svg(items):
    """One path-data string per glyph (units/10, y down) and ink bounds (full units, y down).

    Glyphs must stay separate paths: where two glyphs overlap at a join and their contours
    wind in opposite directions, a single merged path cancels the overlap under the
    nonzero rule and leaves a hole (e.g. the join of ف into ر in آفرینش). Rasterizers fill
    each glyph on its own, so this matches how the font is meant to render."""
    pens = {}
    for it in items:
        pen = pens.setdefault(it['g'], SVGPathPen(None, ntos=lambda v: str(round(v / 10))))
        r = RecordingPen(); r.value = it['cmds']
        sc, ox, oy = it['sc'], it['ox'], it['oy']
        r.replay(TransformPen(pen, (sc, 0, 0, -sc, (1 - sc) * ox + it['dx'], -((1 - sc) * oy + it['dy']))))
    ink = unary_union([i['poly'] for i in items if not i['hole']])
    x0, y0, x1, y1 = ink.bounds
    flipped = affinity.scale(ink, 1, -1, origin=(0, 0))
    return [p.getCommands() for _, p in sorted(pens.items())], (x0, -y1, x1, -y0), flipped


# ---------------------------------------------------------------- page layout

def build(style, lines, author=None, rise='strong', cswh=True, keshideh=True,
          equal=True, layout='auto', title='', desc='', dot_fix=True, mashq=False):
    font = FONTS[style]
    if not font.exists():
        sys.exit(f'missing font {font}')
    px = PX[style]
    feats = ['cswh'] if (style == 'shekasteh' and cswh) else []
    mask = Masker(font, px, feats)
    out = Outliner(font, feats)
    A, Wd, Hd = out.reference_dot()
    report = []
    lines = [' '.join(t.split()) for t in lines]
    if style == 'nastaliq':
        # IranNastaliq's الله ligature draws its own shadda and small alif; typing them too
        # stacks a second set on top (a heavy, doubled shadda)
        fixed = [re.sub('(?<![\u0600-\u06ff])الل[\u064b-\u065f\u0670]+ه', 'الله', t) for t in lines]
        if fixed != lines:
            report.append('الله: written plain; the font\'s ligature already carries the shadda and small alif')
        lines = fixed
    r, start = RISE[rise] if style == 'shekasteh' else (0, 0)
    r = round(r * px / 64)

    gaps = [base_gap(t, px) for t in lines]
    has_manual = any(TATWEEL in t for t in lines)
    auto = style == 'nastaliq' and keshideh and not has_manual
    if style == 'nastaliq' and len(lines) == 2 and equal:
        lines, gaps = equalize(mask, lines, px, report, auto_keshideh=auto)
    elif auto and len(lines) == 1:
        opts = keshideh_options(mask, lines[0])
        if opts:
            lines[0] = opts[0][0]
            report.append(f"keshideh: '{lines[0].split(' ')[opts[0][1]]}'")

    built, line_items = [], []
    for t, g in zip(lines, gaps):
        if mashq:   # tight words, each climbing onto the end of the one before
            pl = pack_line(mask, t, round(g * MASHQ['gap']), slope=MASHQ['slope'], maxtuck=MASHQ['tuck'])
        else:
            pl = pack_line(mask, t, g, rise=r, start=start)
        items = out.contours(pl, px)
        shrink_tashkil(items, TASHKIL_SCALE)
        line_items.append(items)
        built.append(line_svg(items))

    s = 290 / max(b[2] - b[0] for _, b, _ in built)
    stagger = layout == 'stagger' or (layout == 'auto' and len(lines) > 1)
    placed_lines, prev = [], None
    for k, (d, (x0, y0, x1, y1), poly) in enumerate(built):
        if mashq:   # right ends on one column; each line tucks under the one above
            tx = 345 - x1 * s
        elif not stagger:
            tx = 190 - (x1 - x0) * s / 2 - x0 * s
        elif k % 2 == 0:
            tx = 345 - x1 * s
        else:
            tx = 35 - x0 * s
        if prev is None:
            ty = 48 - y0 * s
        elif mashq:
            ty = prev['top'] - y0 * s
            while True:
                cur = affinity.affine_transform(poly, [s, 0, 0, s, tx, ty])
                if all(cur.distance(p['poly']) >= MASHQ['lead'] for p in placed_lines):
                    break
                ty += 1
        else:
            ty = prev['bottom'] + 16 - y0 * s
            while True:
                cur = affinity.affine_transform(poly, [s, 0, 0, s, tx, ty])
                if all(cur.distance(p['poly']) >= 14 for p in placed_lines):
                    break
                ty += 1
        cur = affinity.affine_transform(poly, [s, 0, 0, s, tx, ty])
        prev = dict(d=d, tx=tx, ty=ty, s=s, cls='ink', bottom=ty + y1 * s, top=ty + y0 * s,
                    poly=cur, items=line_items[k])
        placed_lines.append(prev)

    bottom = max(p['bottom'] for p in placed_lines)
    paths = list(placed_lines)
    if author:
        a_items = out.contours(pack_line(mask, ' '.join(author.split()), base_gap(author, px)), px)
        d, (x0, y0, x1, y1), _ = line_svg(a_items)
        sn = s / 2
        tx = 345 - x1 * sn; ty = bottom + 20 - y0 * sn
        paths.append(dict(d=d, tx=tx, ty=ty, s=sn, cls='nm', items=a_items))
        bottom = ty + y1 * sn
    Ht = math.ceil(bottom + 44)

    # settle dots and marks once, over the whole page, in body font units (y up)
    page = []
    for k, p in enumerate(paths):
        f = p['s'] / s
        for it in p['items']:
            gi = dict(it, g=(k, it['g']), src=it, f=f, dx=0.0, dy=0.0)
            gi['poly'] = affinity.translate(affinity.scale(it['poly'], f, f, origin=(0, 0)),
                                            p['tx'] / s, -p['ty'] / s)
            page.append(gi)
    moved, unresolved = settle(page, A, Wd, Hd, room=MASHQ['room'] if mashq else 0.5) if dot_fix else ([], [])
    if mashq and dot_fix:
        moved += [('spread ' + k, ch, w) for k, ch, w in spread_dots(page, A, Wd, Hd)]
        unresolved = settle(page, A, Wd, Hd, passes=0)[1]   # judge the final places, not the stricter target
    for gi in page:
        if gi['dx'] or gi['dy']:
            gi['src']['dx'] += gi['dx'] / gi['f']; gi['src']['dy'] += gi['dy'] / gi['f']
    for p in paths:
        p['d'] = line_svg(p['items'])[0]
    for kind, ch, word in moved:
        report.append(f"moved: {kind} of {ch} in {word}")
    for kind, ch, word in unresolved:
        report.append(f"STILL CROWDED (no clean move found): {kind} of {ch} in {word}")

    c = COLORS
    css = (f".pnl{{fill:{c['paper']}}}.ink{{fill:{c['ink']}}}.nm{{fill:{c['name']}}}"
           f"@media (prefers-color-scheme:dark){{.pnl{{fill:{c['dpaper']}}}.ink{{fill:{c['dink']}}}.nm{{fill:{c['dname']}}}}}"
           f"[data-mode=\"dark\"] .pnl{{fill:{c['dpaper']}}}[data-mode=\"dark\"] .ink{{fill:{c['dink']}}}"
           f"[data-mode=\"dark\"] .nm{{fill:{c['dname']}}}"
           f"[data-mode=\"light\"] .pnl{{fill:{c['paper']}}}[data-mode=\"light\"] .ink{{fill:{c['ink']}}}"
           f"[data-mode=\"light\"] .nm{{fill:{c['name']}}}")
    esc = lambda t: t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="760" height="{2 * Ht}" viewBox="0 0 380 {Ht}" role="img">',
           f'<title>{esc(title or " / ".join(lines))}</title>',
           f'<desc>{esc(desc or ("Persian " + style + " calligraphy, typeset from font outlines"))}</desc>',
           f'<style>{css}</style>',
           f'<rect class="pnl" width="380" height="{Ht}"/>']
    for p in paths:
        svg.append(f'<g class="{p["cls"]}" transform="translate({p["tx"]:.2f} {p["ty"]:.2f}) '
                   f'scale({10 * p["s"]:.5f})">' + ''.join(f'<path d="{d}"/>' for d in p['d'] if d) + '</g>')
    svg.append('</svg>')
    return '\n'.join(svg), report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('lines', nargs='+', help="one argument per misra' / line")
    ap.add_argument('--style', choices=FONTS, default='nastaliq')
    ap.add_argument('--author', help='signature (raqam), drawn at half size in the lower corner')
    ap.add_argument('--rise', choices=RISE, default='strong', help='Shekasteh line-end rise preset')
    ap.add_argument('--no-cswh', action='store_true', help='Shekasteh: turn off fused letters')
    ap.add_argument('--no-keshideh', action='store_true', help='Nastaliq: no automatic keshideh')
    ap.add_argument('--no-equalize', action='store_true', help="don't match the two misra' lengths")
    ap.add_argument('--layout', choices=['auto', 'stagger', 'center'], default='auto')
    ap.add_argument('--mashq', action='store_true',
                    help='master-sheet flow: steep diagonal lines, tucked words, interlocking lines, dots spread into open space')
    ap.add_argument('--no-dot-fix', action='store_true', help='leave every dot and mark where the font puts it')
    ap.add_argument('--title', default='')
    ap.add_argument('--desc', default='')
    ap.add_argument('-o', '--out', required=True, help='output .svg')
    ap.add_argument('--png', help='also write a PNG preview (needs cairosvg)')
    ap.add_argument('--dark', action='store_true', help='PNG preview in dark mode')
    ap.add_argument('--timeout', type=int, default=300, help='seconds before giving up')
    a = ap.parse_args()

    signal.signal(signal.SIGALRM, lambda *_: sys.exit(f'timed out after {a.timeout}s'))
    signal.alarm(a.timeout)

    svg, report = build(a.style, a.lines, a.author, a.rise, not a.no_cswh, not a.no_keshideh,
                        not a.no_equalize, a.layout, a.title, a.desc, not a.no_dot_fix, a.mashq)
    Path(a.out).write_text(svg, encoding='utf-8')
    print(f'wrote {a.out}')
    if a.png:
        import cairosvg
        src = svg.replace('role="img">', f'role="img" data-mode="{"dark" if a.dark else "light"}">', 1)
        cairosvg.svg2png(bytestring=src.encode(), write_to=a.png, output_width=1520)
        print(f'wrote {a.png}')
    for line in report:
        print(' ', line)


if __name__ == '__main__':
    main()
