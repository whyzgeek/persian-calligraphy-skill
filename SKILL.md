---
name: "persian-calligraphy"
description: "Render Persian poetry or text as Nastaliq or Shekasteh-Nastaliq calligraphy: licensed fonts, verse layout, kashida placement, spacing. Use for Hafez, Rumi, Saadi, calligraphy or khat requests."
---

# Persian calligraphy (Nastaliq & Shekasteh)

The goal is a piece a Persian reader finds correct and beautiful, not just "Arabic-looking" text. The quality comes from the right font and text, words spaced by their ink, dots and marks that never touch another letter, well-placed keshideh, and generous space. Being honest about what a font can and cannot do matters as much as the look.

## Quick start

```bash
pip install -r requirements.txt          # Pillow needs libraqm: python -c "import PIL.features as f; print(f.check('raqm'))"

# a bayt: one argument per misra'
python scripts/render.py --style nastaliq "بنی آدم اعضای یکدیگرند" "که در آفرینش ز یک گوهرند" \
    --author "سعدی" -o saadi-nastaliq.svg --png preview.png

python scripts/render.py --style shekasteh "بشنو این نی چون شکایت می‌کند" "از جدایی‌ها حکایت می‌کند" \
    --author "مولانا" --rise strong -o rumi-shekasteh.svg --png preview.png
```

The script applies every rule in sections 4 to 7 and prints a report: the keshideh it chose, the gaps, and each dot or mark it moved. Check the PNG preview before you send anything (section 8). Options: `--rise flat|gentle|medium|strong|stronger`, `--no-cswh`, `--no-keshideh`, `--no-equalize`, `--layout auto|stagger|center`, `--dark` (preview only), `--title`, `--desc`. If you type a tatweel (ـ) yourself, the script uses it and skips automatic keshideh.

## Output

Always deliver a standalone **SVG vector file** made from the font's own glyph outlines, and display or attach it for the user. Put the translation and notes in the reply, not in the picture.

Both styles use the same design, a **bare manuscript sheet**: warm paper (#f3e8d3), deep burgundy ink (#5a1a1f), no frame and no ornaments. If the text has an author, their name goes small in the lower corner, at **half the main text's letter height and lighter in tone** (#6e2a2a), like a calligrapher's signature (raqam) rather than a second heading. Dark mode: paper #241a16, ink #ecd3b2, name #d9b48a. The SVG switches through `prefers-color-scheme` or `data-mode="dark"` on its root.

## 1. The text

- Classical poems have variant readings across editions. Use the best-known reading, say which edition or reading you used, and name the main alternative in one line. Don't invent a word you're unsure of.
- A bayt has two misra', staggered: the first right-aligned, the second left-aligned. A phrase that isn't a bayt (a Bismillah, a saying, a name) is one centred line. Longer prose or scripture can be split into clauses (at a ؛, a comma, or a verse's natural pause points) and use the same staggered layout over as many lines as needed. Only real bayt poetry needs equal line lengths (`--no-equalize` otherwise).
- Keep the ZWNJ (نیم‌فاصله, U+200C) in words like می‌کند and دل‌ها. It is correct orthography, and those letters don't join.
- Vowel marks (tashkil): none by default, since they crowd the strokes, especially in Shekasteh. On Arabic phrases, or when the user asks, prefer **light marks**: shaddas and the small alif of اللّٰه. Offer full tashkil as an option. Fonts sometimes place a mark far above a tall fused shape; offer the plain-fatha spelling as the alternative, and never place marks by hand.
- Say in the reply when the text is Arabic. For scripture, check the wording against a primary source before laying it out; getting a sacred text wrong is worse than getting a poem wrong.

## 2. Fonts

Both fonts are bundled in `fonts/` with their licenses.

| Style | Font | License | Source |
|---|---|---|---|
| Nastaliq | `fonts/IranNastaliq.ttf` (v1.5) | OFL-1.1, Iran IT Organization | [farsi-fonts/fonts-irannastaliq](https://github.com/farsi-fonts/fonts-irannastaliq) |
| Shekasteh | `fonts/DimaShekastehFree.ttf` (v2.001) | GPL v2, R. Balvardi / Dima Group | [ahangarha/dima](https://github.com/ahangarha/dima) |

- Nastaliq means IranNastaliq, and Shekasteh (شکسته) means Dima Shekasteh. Don't swap in another font without asking.
- Commercial fonts (Chalipa, Zarnegar and others) can't be embedded. Say why in one line.
- For any font the user supplies, first read its name table for copyright and license (IDs 0, 1, 13). Then test that its joins and keshideh are clean, and show numbered variants before adopting it. The user wants a font that can draw a smooth, toothless keshideh.

```python
from fontTools.ttLib import TTFont
for r in TTFont(path)['name'].names:
    if r.nameID in (0, 1, 13): print(r.nameID, r.toUnicode())
```

Some font features aren't defects, so leave them as drawn: Dima's hollow ا, the fine hairlines in its long swashes (reed-pen edges), and the open space inside its long ی bowl. If something looks hollow or odd, render it large with FreeType (Pillow). If it looks the same there, it is the font's design.

## 3. Honesty

No font reproduces a calligrapher's hand. Never hand-draw Arabic letters or joins as SVG paths, because that produces wrong letters. Converting the font's own outlines to paths is fine, and so is moving a whole dot, a whole mark (madda, hamza) or a whole word. Don't redraw dot shorthand or stretch strokes beyond the font's own shapes. Say in the reply that the piece is typeset, not hand-written. If the user attaches an image of real calligraphy, tracing it (e.g. with potrace) is another option.

## 4. Style rules

### Nastaliq

- **Straight baseline.** Don't lift or drop words; drift looks random in Nastaliq.
- **Keshideh (tatweel ـ).** A single tatweel at a valid joint makes IranNastaliq substitute a designed long letter.
  - Use exactly one tatweel per word. With two or more, the font falls back to plain letters.
  - **Where it goes.** A keshideh stretches the second-to-last letter of a connected run into the run's last letter, and never the word's first letter. **ص ض** may stretch at the end of any run (اعضـای). **ب پ ت ث ن ی ئ ف ق ک گ** stretch only into the word's final letter, or the last letter before a ZWNJ (الرحیـم, مشکـل‌ها).
  - **Never stretch س or ش.** In Nastaliq the keshideh itself stands for the sin; IranNastaliq draws the teeth and then the stretch, which reads as sin + keshideh (کأسـا, بسـم). Also never stretch a word's first letter (نـاولها reads as ن + اولها), ل (الـرحیم reads as السرحیم), ه, ح ج چ خ, م, ع غ, or a ک/گ in the middle of a word (یکـدیگرند cuts the word in two). A stretch into a short final letter of a long word can look out of proportion (یکدیگرنـد), so prefer ص/ض when the misra' has one. If no word has a place, give the misra' no keshideh, widen its gaps, and say in the reply if the misra' still differ in length.
  - Keep it only if the word gets noticeably wider (about +30% or more) and looks right.
  - Use it only on content words, never on particles or one-joint words (به, که, را, و, یا, چه).
  - Allow at most one per misra'. Among places of the same kind, prefer the last content word, so the line closes on the long stroke.
  - Use only the length the font's shaping chooses. Swapping in the font's longer glyphs by hand breaks the joins.
- **Equal misra'.** Pack both misra' plain. Give the shorter one a keshideh (by the rules above), then widen only that misra's word gap until both ink widths match within about 1%. A keshideh typed by hand is kept, and the gaps are still matched.
- **Single line.** One keshideh by the same rules, centred.
- `cswh` does almost nothing in IranNastaliq, so leave it off.

### Shekasteh

- **No manual keshideh.** Dima's shaping already draws flowing joins, and a forced tatweel makes squeezed loops.
- **Fused letters (on).** Dima's `cswh` feature fuses pairs such as شک and حک the way a Shekasteh hand does. Turn it off (`--no-cswh`) if the user wants easier reading.
- **Rising line end (on).** The line runs on the baseline and lifts at the end of each misra', like the bowl of ن or س turning up. Whole words are raised, never rotated: a word at position `t` (0 = first, 1 = last) past `start` rises by `rise × ((t − start)/(1 − start))²`.

| Preset | rise (px at 64px) | start |
|---|---|---|
| flat | 0 | – |
| gentle | 24 | 0.45 |
| medium | 40 | 0.40 |
| **strong (default)** | **56** | **0.35** |
| stronger | 72 | 0.30 (check visually) |

- Don't tilt a whole misra' diagonally. Stack words into each other's bowls only if the user asks for a more hand-composed look.
- Dima's long final swashes (ن, ی) often run over the letter before them, so a mark on that letter (the madda of آ in آن, for one) can land on the swash. Section 6 handles this.

## 5. Word spacing by ink

Every word gets the same space character, but word shapes are uneven, so gaps look uneven. Space by ink instead (`pack_line`): place words right to left, sliding each toward the text already placed until its ink is a fixed clearance away.

- **Base gap ≈ 0.2 em** (12px at 56 to 64px).
- **Step up to about 0.25 em** when several words end in a non-forward-joiner (د ر ز و ا) or the line has one-letter words; otherwise they read as one word.
- About 0.1 em runs words together. A misra' carrying a keshideh can take a wider gap (up to ~0.4 em) and still look balanced.
- A word may tuck under its neighbour, but its left edge must stay at least 35% of its width left of the previous text's left edge.
- Don't split words at a ZWNJ.
- Watch pairs where a ر tail slides under a following ع. They read, but look close.

## 6. Dots and marks that don't touch other letters

Both fonts draw dots and marks (madda, hamza) inside each letter's glyph, so a neighbouring stroke can land on them. **Fix them in one pass over the finished page (`settle`), not word by word:** lay out every line first with all glyphs as the font draws them, then check every dot and mark against all ink on the page (neighbours, other words, the other misra', dots already moved), move only the ones that really collide, and repeat until nothing changes. Per-line rules miss collisions and, when tuned, shift dots elsewhere. To review, render with and without the pass (`--no-dot-fix`) and look at every region that changed.

- **Identify before fixing.** When a user reports a collision, render the word with each contour in its own colour and list each contour's glyph, character, area and bounds (`Outliner.contours`). Don't assume which shape is which: in Dima's آن the diamond is the ن's dot and the madda is a separate wave lying on the ن's swash.
- **Dot group:** a free contour whose area is within 20% of 1, 2 or 3 dots. One dot's size is learned from the font. A contour with a counter (head of ف), one touching its own letter, or one alone in its glyph is not a dot.
- **Marks:** only on آ أ إ ؤ ئ ۀ: the free contour of at most ~4 dots' area, smaller than half the letter's body, lying wholly above the body (below for إ). In IranNastaliq the alef of أ is a separate contour; moving it instead of the hamza detaches the ا from the ک (کأسا → «ا کسا»).
- **No more dots than the letter has.** A dot group can't hold more dots than its letter carries (ف 1, ی 2, ش 3 …). IranNastaliq draws the ف head as a separate solid piece about two dots in size; since ف has one dot, that piece is the head, and moving it lifts the ف off the ر (آفرینش). The two dots of ی, drawn as one piece, remain eligible. Don't use "largest piece of its glyph" as the body test: the ی's dot pair is bigger than its tooth.
- **Stay home.** A moved dot or mark must stay horizontally over its own letter and get no further from it than it started. Otherwise it changes the word: a ن dot pushed over the next د makes گوهرند read «گوهرنز». Don't require it to be nearer its own letter than to any other: Nastaliq hangs the ی's dots far below its tooth, right on a following ر (آفرینش), and they must still be movable.
- **Trigger:** closer than half a dot's height to *another* letter's ink (slivers under 0.8 dot are ignored). Distance to its own letter is the designer's choice.
- **Move** until 1.4 × that clearance away. Dots search the whole circle, starting with the side away from their own letter, and move at most ~1.3 dot heights. Marks search ~3 dot heights, stay near their letter and keep at least 0.3 dot heights from it.
- **Move a letter's dots together.** All dots of one letter move as one group and keep their arrangement. Dima draws each dot of ش or ی as its own piece; moving them one by one merges them into a blob.
- **Never make it worse.** If no move clears it while staying with its letter, leave it where it is; the script reports it as `STILL CROWDED` so you can mention it.

## 7. Layout (viewBox 380 wide)

- **One `<path>` per glyph**, never one merged path per line. At a join, two glyphs overlap; if their contours wind in opposite directions, a merged path cancels the overlap under the nonzero rule and cuts a hole into the join (فر in آفرینش looked broken). Rasterizers fill each glyph on its own, and so must the SVG.

- A full-size paper rect and nothing else behind the text. One scale for all lines: the longest line is 290 wide.
- **Bayt / multi-line:** line 1 right edge at x=345, ink top at y=48. Line 2 left edge at x=35, 16px below line 1's lowest ink, moved down further until no ink of the two lines is closer than 14px. Further lines alternate the same way.
- **Single line:** centred, ink top at y=48.
- **Author / citation:** built the same way at **half the body scale**, right edge at x=345, 20px below the lowest ink. Page height = last ink + 44. Keep a citation to words only: a bare `chapter:verse` numeral in an RTL line can reorder under bidi.
- Name the file after the text and style, e.g. `hafez-ghazal1-nastaliq.svg`.

## 8. Check before sending

- Look at the PNG preview (`--png`). Check that no dot or mark touches another letter, every madda and hamza shows as its own shape, no ink touches another line, the stagger reads clearly, Nastaliq misra' are equal, the Shekasteh line end rises smoothly, gaps look even, and the signature reads visibly smaller than the body.
- Check each word with a madda or hamza at 3× zoom. A mark lying on a swash reads as a thick blob, not as a collision.
- Read the script's report: is the keshideh word sensible, and is every moved dot one that was really crowded? Compare before and after at normal size. If you can't see the difference, the fix is aimed at the wrong piece.
- When the user is choosing between looks, build numbered variants, send a numbered side-by-side image plus each SVG, and keep the numbering going across rounds.

## 9. The reply

- A faithful English translation; note any Arabic parts.
- The reading used, if the text has known variants; for scripture, the source you checked.
- The font, and that the letters are the font's own shapes as vector paths, spaced by ink, with crowded dots and marks moved clear. For Nastaliq, name the keshideh word; for Shekasteh, mention the fused letters and the rising end (the user can ask for flat or more rise).
- The limitation: typeset, not hand-written.

Offer one next step: another verse, a different ink colour, or a larger print version.
