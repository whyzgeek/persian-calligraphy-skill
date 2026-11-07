#!/usr/bin/env python3
"""Rebuild every image in samples/ from the exact commands recorded below.

    python scripts/make_samples.py           # rewrite samples/*.svg and *.png
    python scripts/make_samples.py --check   # render to a temp dir, compare with samples/

--check is the regression test for any change to render.py: when you fix one reported
issue, every sample that should not change must come out byte-identical. If one differs,
look at the changed region (before/after crops) before accepting it.
"""
import argparse
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RENDER = ROOT / 'scripts' / 'render.py'
OUT = ROOT / 'samples'
STEP_TIMEOUT = 300          # seconds per render
TOTAL_TIMEOUT = 1800        # seconds for the whole run

RUMI = ["بشنو این نی چون شکایت می‌کند", "از جدایی‌ها حکایت می‌کند"]
SAADI = ["بنی آدم اعضای یکدیگرند", "که در آفرینش ز یک گوهرند"]

# name -> (render.py arguments, also write a dark-mode PNG)
SAMPLES = {
    'hafez-nastaliq': (['--style', 'nastaliq', 'الا یا ایها الساقی ادر کأسا و ناولها',
                        'که عشق آسان نمود اول ولی افتاد مشکل‌ها', '--author', 'حافظ'], False),
    'saadi-nastaliq': (['--style', 'nastaliq', *SAADI, '--author', 'سعدی'], True),
    'rumi-shekasteh': (['--style', 'shekasteh', *RUMI, '--author', 'مولانا'], True),
    'bismillah-tashkil-nastaliq': (['--style', 'nastaliq', 'بِسمِ الله الرَّحمن الرَّحیـم'], False),
    'rumi-mashq-shekasteh': (['--style', 'shekasteh', '--mashq', *RUMI,
                              'کز نیستان تا مرا ببریده‌اند', 'در نفیرم مرد و زن نالیده‌اند',
                              '--author', 'مولانا'], False),
    'saadi-mashq-nastaliq': (['--style', 'nastaliq', '--mashq', *SAADI, '--author', 'سعدی'], False),
}

# Shekasteh rise presets, stacked with labels into one image
RISE_PRESETS = [('flat', 'flat'), ('gentle', 'gentle'), ('strong', 'strong (default)'), ('stronger', 'stronger')]


def render(args, svg, png=None, dark=False):
    cmd = [sys.executable, str(RENDER), *args, '-o', str(svg), '--timeout', str(STEP_TIMEOUT)]
    if png:
        cmd += ['--png', str(png)] + (['--dark'] if dark else [])
    subprocess.run(cmd, check=True, timeout=STEP_TIMEOUT + 30, stdout=subprocess.DEVNULL)


def rise_sheet(tmp):
    """One SVG: each preset's render nested at full width under a small label."""
    parts, y = [], 0
    for preset, label in RISE_PRESETS:
        f = tmp / f'rise-{preset}.svg'
        render(['--style', 'shekasteh', '--rise', preset, RUMI[0]], f)
        src = f.read_text(encoding='utf-8')
        h = float(re.search(r'viewBox="0 0 380 ([\d.]+)"', src).group(1))
        inner = re.sub(r'^<svg [^>]*>', f'<svg x="0" y="{y + 22}" width="380" height="{h}" viewBox="0 0 380 {h}">', src)
        parts.append(f'<text x="12" y="{y + 16}" font-family="sans-serif" font-size="11" fill="#6e2a2a">{label}</text>')
        parts.append(inner)
        y += h + 22
        parts.append(f'<line x1="0" x2="380" y1="{y}" y2="{y}" stroke="#d9c7a7" stroke-width="0.6"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="760" height="{2 * y:.0f}" viewBox="0 0 380 {y:.0f}">'
            f'<rect width="380" height="{y:.0f}" fill="#f3e8d3"/>' + ''.join(parts) + '</svg>')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true', help='compare fresh renders with samples/ instead of writing')
    a = ap.parse_args()
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(f'timed out after {TOTAL_TIMEOUT}s'))
    signal.alarm(TOTAL_TIMEOUT)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        if a.check:
            changed = []
            for name, (args, _) in SAMPLES.items():
                render(args, tmp / f'{name}.svg')
                same = (tmp / f'{name}.svg').read_bytes() == (OUT / f'{name}.svg').read_bytes()
                print(f"{'identical' if same else 'CHANGED  '}  {name}.svg")
                changed += [] if same else [name]
            sys.exit(1 if changed else 0)

        import cairosvg
        for name, (args, dark) in SAMPLES.items():
            render(args, OUT / f'{name}.svg', OUT / f'{name}.png')
            if dark:
                render(args, tmp / 'dark.svg', OUT / f'{name}-dark.png', dark=True)
            print(f'wrote {name}')
        cairosvg.svg2png(bytestring=rise_sheet(tmp).encode(), write_to=str(OUT / 'rise-presets.png'),
                         output_width=1520)
        print('wrote rise-presets')


if __name__ == '__main__':
    main()
