---
name: prestaff
description: Render beginner piano method-book pages — pre-staff finger-number notation with noteheads, stems, lyrics and hand labels, plus conventional staff systems — to SVG/PDF/PNG from a tiny YAML notation. Use when adapting or recreating a piece for a child's lesson book, or matching the look of a specific method book page.
---

# prestaff

Renders the pre-staff notation beginner piano books use before students read a
staff: real noteheads and stems, no staff lines, finger numbers, lyrics, hand
labels. Also renders conventional staff systems for the teacher-duet line at the
foot of such a page.

`prestaff.py` is the renderer, `measure.py` is how you check yourself against a
photo of the original.

## Run it

```sh
uv run --with pyyaml --with cairosvg prestaff.py song.yaml out.svg --png --pdf
```

Output is SVG at true page geometry — coordinates are millimetres and the
viewBox is the page — so the PDF prints at real size. Attach the PNG when
showing someone; Zulip will not thumbnail an SVG.

## The notation

A note is `<finger><duration>`, duration `q` quarter, `h` half, `w` whole,
`e` eighth. An optional `+n` / `-n` shifts position when the hand moves off its
home group.

```yaml
systems:
  - y: 84
    lyric_pos: below          # 'above' when the row is an L.H. row
    groups:
      - {hand: RH, dyn: f, seq: "2q 2q 3q 3q 4h 4h"}
      - {hand: RH, dyn: p, seq: "2q 2q 3q 3q 4h 4h", gap: 10}
    lyrics: ["Shout a- cross the val- ley, Now I hear the ech- o."]
    barline: true             # or `repeat: true`
```

One lyric syllable per note, in playing order **across all groups** — the lyric
is a single stream and the hands trade off inside it. A trailing `-` draws the
hyphen to the next note.

Everything else is derived. Do not hand-place it.

## The rules this encodes

These came out of measuring real pages. They are not stylistic choices.

- **Stem direction is the hand.** R.H. stems up on the right, finger number
  above. L.H. stems down on the left, finger number below.
- **Finger number prints only when it changes.** `4q 4q 3q 3q 2w` shows 4, 3, 2.
- **The finger number IS the pitch.** The hand sits in one fixed position, so
  R.H. rises with the finger and **L.H. falls with it** — a left hand fingered
  2-3-4 runs downward across the keys. Getting this backwards silently inverts
  every melody.
- **One pitch step is 0.4-0.5 notehead heights.** Set per page via `pitch_step`;
  books are not consistent between pages, so measure the page you are copying.
- **L.H. rows put lyrics above the notes**, because the finger numbers occupy
  the space below.
- Hand-change arrows are drawn automatically wherever the hand changes inside a
  system. Rows that change hand between systems get no arrow.

## Staff systems

For the teacher duet. Pitch tokens are `<letter><accidental><octave><duration>`,
`|` is a barline.

```yaml
staff:
  x: 18
  y: 172                      # y of the TOP staff line
  width: 258
  clef: bass
  flats: 6
  time: "4/4"
  voices:
    - stem: up
      slurs: [[0, 5]]
      fingers: {"0": "3"}
      seq: "Bb3q Bb3q Bb3q Bb3q | Db4h Cb4h"
    - stem: down
      seq: "Ab2h Db3h | Gb2q Db3q Eb3q Db3q"
```

Notes are spaced **per beat**, not per note, so voices stay vertically aligned.
Ledger lines are automatic. Music glyphs come from FreeSerif (bass clef
U+1D122, flat U+266D) — check `fc-list ":charset=1D122"` on a new host.

## Recreating a page from a photo — the loop

This is the part that matters. Do not eyeball a photo and declare a match.

1. Render.
2. Rasterise and **look at your own output**.
3. Crop the same region from the original photo, scale both to the same width,
   stack them with labels, and look at the pair.
4. Fix the largest divergence. Go back to 1.

Repeat until the differences are ones you can name and defend.

### Measure, do not guess

```sh
uv run --with pillow --with scipy --with numpy measure.py heads photo.jpg 640,2280,2760,2800 dbg.png
uv run --with pillow --with scipy --with numpy measure.py staff photo.jpg 150,2830,2700,3300 dbg.png
```

Then run the same tool over your own rendered PNG and compare the ratios —
pitch step in notehead-heights, hand separation, spacing. That is the check.

**De-tilt first.** A hand-held photo drifts several pixels per note and that
drift is indistinguishable from a descending melody. Find notes you know are
the same pitch — same finger, same hand — measure the drift across them, and
subtract. A run of three notes on finger 2 that "descends" is the page tilting,
not the tune.

Confirmation that the grid is right: repeated musical figures must land on
identical measured positions. If they do not, the grid is wrong.

### Known limit

Pitch resolution off a curved phone photo is about ±0.4 staff positions, and a
half space is the gap between adjacent notes. Structure — clef, key, metre,
bar count, rhythms, phrasing — is recoverable. Individual pitches in a dense
staff system are not certifiable. Say so rather than inventing them, and ask
for a straight-on close-up of that system.

## Examples

`examples/old-macdonald.yaml` — hands alternating inside one system, hand-change
arrows, display lyrics, repeat sign.

`examples/i-hear-the-echo.yaml` — two systems (R.H. forte, L.H. echoing piano),
lyrics above and below, generalised keyboard diagram, Discovery box with inline
rhythm glyphs, and a full bass-clef teacher duet.

## Page furniture

`title`, `practice_steps`, `intro`, `terms`, `bullet`, `eye_check`, `discovery`
(with `rhythm:` drawn as real glyphs), `callout`, `page`, and `find_keys`:

```yaml
find_keys:
  groups: [3, 2, 3]                 # black-key groups, left to right
  labels: [[4, 3, 2], [], [2, 3, 4]]
  brackets:
    - {group: 0, text: "L.H."}
    - {group: 2, text: "R.H."}
```

Positions are explicit millimetres (`title_y`, `system_y`, …). There is no
auto-layout — render and look.

## Copyright

Recreating a published page is for a specific child's own book. Do not
reproduce the publisher's illustrations, and do not redistribute a recreated
page as if it were the book.
