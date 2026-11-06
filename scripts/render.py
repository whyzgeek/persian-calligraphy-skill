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
MARKED = set('آأإؤئۀ')                     # letters that carry a madda or hamza
SIN_FAMILY = set('سشصض')                    # the classical keshideh letters
STRETCHABLE = set('بپتثنیئفق')              # also stretchable, but only into a word's final letter
HARAKAT = set(chr(c) for c in range(0x064B, 0x0660)) | {'ٰ'}

W, H, BY = 5000, 800, 480                   # mask canvas (px) and its baseline

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


def lifts(mask, words, rise, start):
    if not rise:
        return [0] * len(words)
    wid = [max(1, mask.width(w)) for w in words]
    tot, run, out = sum(wid), 0, []
    for wd in wid:
        t = (run + wd / 2) / tot
        run += wd
        out.append(-round(rise * max(0, (t - start) / (1 - start)) ** 2))
    return out


def pack_line(mask, text, clear, rise=0, start=0.35, maxtuck=0.35):
    """Place words right to left, each sliding in until its ink is `clear` px from the
    text already placed. Returns [(word, x_px, dy_px)]; dy negative = up."""
    words = text.split(' ')
    out, acc = [], None
    for w, dy in zip(words, lifts(mask, words, rise, start)):
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
      tier 1: the sin family (س ش ص ض) at the end of any run: کأسـا، اعضـای.
      tier 2: ب پ ت ث ن ی ئ ف ق into the word's final letter only: الرحیـم.
    ک and گ are left alone: IranNastaliq's stretched kaf cuts a word in two (یکـدیگرند),
    and ل, ه, the ح family, م and ع draw badly stretched (الـرحیم reads as السرحیم)."""
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
        tier = 1 if c in SIN_FAMILY else (2 if c in STRETCHABLE and k == len(runs) - 1 else 0)
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
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
                rec = DecomposingRecordingPen(self.gs)
                self.gs[self.order[info.codepoint]].draw(
                    TransformPen(rec, (1, 0, 0, 1, x + pos.x_offset, y0 + pos.y_offset)))
                cur = []
                for op, args in rec.value:
                    cur.append((op, args))
                    if op in ('closePath', 'endPath'):
                        r = RecordingPen(); r.value = cur; fp = FlatPen(); r.replay(fp); cur = []
                        if fp.polys:
                            items.append(dict(cmds=r.value, poly=Polygon(fp.polys[0]).buffer(0), g=g,
                                              ch=word[info.cluster], dx=0.0, dy=0.0))
                g += 1
                x += pos.x_advance
        for it in items:   # a contour inside a bigger contour of the same glyph is a counter
            it['hole'] = any(o is not it and o['g'] == it['g'] and o['poly'].area > it['poly'].area
                             and o['poly'].contains(it['poly']) for o in items)
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


def other_ink(ink, it, A):
    """Other letters' ink to clear; slivers under 0.8 dot are decorative and ignored."""
    return unary_union([o['poly'] for o in ink if o['g'] != it['g'] and o['poly'].area >= 0.8 * A])


def stays_home(p, ownu, others, Wd):
    """A moved dot or mark must still read as its own letter's: horizontally over that
    letter, and nearer to it than to any other letter. Otherwise ن+د can read as ن+ذ."""
    x0, _, x1, _ = ownu.bounds
    cx = p.centroid.x
    return x0 - 0.3 * Wd <= cx <= x1 + 0.3 * Wd and ownu.distance(p) < others.distance(p)


def fix_dots(items, A, Wd, Hd, step=10, margin=1.4):
    clear = 0.5 * Hd; target = clear * margin; maxmove = 1.3 * Hd
    ink = [i for i in items if not i['hole']]; moved = []
    for it in ink:
        if not is_dot_group(it['poly'], A, Wd, Hd) or has_counter(it, items):
            continue
        own = [o['poly'] for o in ink if o['g'] == it['g'] and o is not it]
        if not own or any(p.distance(it['poly']) == 0 for p in own):
            continue
        others = other_ink(ink, it, A)
        if others.is_empty:
            continue
        d0 = others.distance(it['poly'])
        if d0 >= clear:
            continue
        ownu = unary_union(own)
        below = it['poly'].centroid.y < ownu.centroid.y; s = -1 if below else 1
        pref = [(math.sin(math.radians(a)), s * math.cos(math.radians(a))) for a in range(-90, 91, 15)]
        rest = [(math.sin(math.radians(a)), s * math.cos(math.radians(a))) for a in range(105, 271, 15)]
        best = None
        for dirs in (pref, rest):
            for ux, uy in dirs:
                r = step
                while r <= maxmove:
                    p = affinity.translate(it['poly'], ux * r, uy * r)
                    if others.distance(p) >= target and stays_home(p, ownu, others, Wd):
                        if best is None or r < best[0]:
                            best = (r, ux * r, uy * r)
                        break
                    r += step
            if best is not None:
                break
        if best is None:
            continue   # no move clears it without leaving its letter: keep the designer's spot
        it['dx'], it['dy'] = best[1], best[2]
        it['poly'] = affinity.translate(it['poly'], best[1], best[2]); moved.append((it['ch'], 'dot'))
    return moved


def fix_marks(items, A, Wd, Hd, step=20, reach=3.0, margin=1.4):
    """Madda/hamza of آ أ إ ؤ ئ ۀ lying on another letter: smallest nearby move that clears it."""
    clear = 0.5 * Hd; target = clear * margin
    ink = [i for i in items if not i['hole']]; moved = []
    for it in ink:
        if it['ch'] not in MARKED or it['poly'].area > 4 * A or is_dot_group(it['poly'], A, Wd, Hd) \
                or has_counter(it, items):
            continue
        own = [o['poly'] for o in ink if o['g'] == it['g'] and o is not it]
        if not own or any(p.distance(it['poly']) == 0 for p in own):
            continue
        ownu = unary_union(own); mb = it['poly'].bounds; ob = ownu.bounds
        # the mark is the small piece, on its proper side: below for إ, above for the rest.
        # (In IranNastaliq the alef of أ is its own contour; never mistake it for the hamza.)
        body = max(own, key=lambda p: p.area)
        if it['poly'].area > 0.5 * body.area:
            continue
        above, below = mb[1] > ob[3], mb[3] < ob[1]
        if not ((below and it['ch'] == 'إ') or (above and it['ch'] != 'إ')):
            continue
        others = other_ink(ink, it, A)
        if others.is_empty or others.distance(it['poly']) >= clear:
            continue
        cx = it['poly'].centroid.x; ocx = ownu.centroid.x; R = reach * Hd; n = int(R // step); best = None
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                dx, dy = i * step, j * step; r = math.hypot(dx, dy)
                if r > R:
                    continue
                cost = r + 0.5 * abs(cx + dx - ocx)
                if best and cost >= best[0]:
                    continue
                p = affinity.translate(it['poly'], dx, dy)
                if others.distance(p) >= target and ownu.distance(p) >= 0.3 * Hd \
                        and stays_home(p, ownu, others, Wd):
                    best = (cost, dx, dy)
        if best is None:
            continue
        it['dx'] += best[1]; it['dy'] += best[2]
        it['poly'] = affinity.translate(it['poly'], best[1], best[2]); moved.append((it['ch'], 'mark'))
    return moved


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
        r.replay(TransformPen(pen, (1, 0, 0, -1, it['dx'], -it['dy'])))
    ink = unary_union([i['poly'] for i in items if not i['hole']])
    x0, y0, x1, y1 = ink.bounds
    flipped = affinity.scale(ink, 1, -1, origin=(0, 0))
    return [p.getCommands() for _, p in sorted(pens.items())], (x0, -y1, x1, -y0), flipped


# ---------------------------------------------------------------- page layout

def build(style, lines, author=None, rise='strong', cswh=True, keshideh=True,
          equal=True, layout='auto', title='', desc=''):
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

    built = []
    for t, g in zip(lines, gaps):
        placed = pack_line(mask, t, g, rise=r, start=start)
        items = out.contours(placed, px)
        mv = fix_dots(items, A, Wd, Hd) + fix_marks(items, A, Wd, Hd)
        if mv:
            report.append(f"moved in '{t}': " + ', '.join(f'{k} of {c}' for c, k in mv))
        built.append(line_svg(items))

    s = 290 / max(b[2] - b[0] for _, b, _ in built)
    stagger = layout == 'stagger' or (layout == 'auto' and len(lines) > 1)
    placed_lines, prev = [], None
    for k, (d, (x0, y0, x1, y1), poly) in enumerate(built):
        if not stagger:
            tx = 190 - (x1 - x0) * s / 2 - x0 * s
        elif k % 2 == 0:
            tx = 345 - x1 * s
        else:
            tx = 35 - x0 * s
        if prev is None:
            ty = 48 - y0 * s
        else:
            ty = prev['bottom'] + 16 - y0 * s
            while True:
                cur = affinity.affine_transform(poly, [s, 0, 0, s, tx, ty])
                if all(cur.distance(p['poly']) >= 14 for p in placed_lines):
                    break
                ty += 1
        cur = affinity.affine_transform(poly, [s, 0, 0, s, tx, ty])
        prev = dict(d=d, tx=tx, ty=ty, s=s, cls='ink', bottom=ty + y1 * s, poly=cur)
        placed_lines.append(prev)

    bottom = max(p['bottom'] for p in placed_lines)
    paths = list(placed_lines)
    if author:
        a_items = out.contours(pack_line(mask, ' '.join(author.split()), base_gap(author, px)), px)
        fix_dots(a_items, A, Wd, Hd); fix_marks(a_items, A, Wd, Hd)
        d, (x0, y0, x1, y1), _ = line_svg(a_items)
        sn = s / 2
        tx = 345 - x1 * sn; ty = bottom + 20 - y0 * sn
        paths.append(dict(d=d, tx=tx, ty=ty, s=sn, cls='nm'))
        bottom = ty + y1 * sn
    Ht = math.ceil(bottom + 44)

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
                        not a.no_equalize, a.layout, a.title, a.desc)
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
