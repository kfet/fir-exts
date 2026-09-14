#!/usr/bin/env python3
"""
prestaff — render pre-staff piano pages (method-book style) from a tiny
structured notation, to SVG.

Input is YAML. The notation itself is one compact string per hand-group:

    "2q 2q 2q"      finger 2, three quarter notes
    "3q 2q 2q 3h"   ... ending on a half note
    "4q 3q 2w"      ... ending on a whole note

    <finger><duration>   finger = 1..5, duration = q | h | w | e

Finger numbers are printed only when the finger changes, exactly as the
book does. Lyrics are one syllable per note, in playing order across all
groups; a trailing '-' or leading '-' on a syllable draws the hyphen.

Usage:  prestaff.py song.yaml out.svg [--png] [--pdf]

Run it with uv so the deps are ephemeral:
    uv run --with pyyaml --with cairosvg prestaff.py song.yaml out.svg --png
"""
import sys
import yaml

# ----------------------------------------------------------------- geometry
# All coordinates are millimetres; the SVG viewBox is the page in mm.
PAGE_W, PAGE_H = 297.0, 210.0

HEAD_RX, HEAD_RY = 1.85, 1.35      # notehead radii
HEAD_TILT = -20                    # degrees
STEM_LEN = 9.0
STEM_W = 0.42
WHOLE_SCALE = 1.28                 # whole notes are a touch wider

ADV = {'e': 8.0, 'q': 11.5, 'h': 15.0, 'w': 19.0}   # horizontal advance
HAND_GAP = 15.6                   # R.H. baseline to L.H. baseline
# Measured from the book: one scale step is ~0.38 notehead-heights. The hand
# sits in a fixed position, so the finger number IS the pitch: R.H. rises with
# the finger, L.H. falls with it.
PITCH_STEP = 1.35        # 0.5 notehead-heights, as measured
LYRIC_DY = 6.2                     # first lyric line below the R.H. head
LYRIC_LEAD = 4.6
LYRIC_ABOVE = 7.0                  # lyric baseline above an L.H. row
FINGER_DY = 3.2                    # above stem top / below stem bottom

SERIF = 'Liberation Serif, Times New Roman, serif'
INK = '#111111'


def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


class Canvas:
    def __init__(self):
        self.out = []

    def add(self, s):
        self.out.append(s)

    def text(self, x, y, s, size=3.5, weight='normal', style='normal',
             anchor='start', fill=INK, family=SERIF, spacing=None):
        sp = ' letter-spacing="%.2f"' % spacing if spacing else ''
        self.add('<text x="%.2f" y="%.2f" font-family="%s" font-size="%.2f" '
                 'font-weight="%s" font-style="%s" text-anchor="%s" fill="%s"%s>%s</text>'
                 % (x, y, family, size, weight, style, anchor, fill, sp, esc(s)))

    def line(self, x1, y1, x2, y2, w=0.3, col=INK, cap='butt'):
        self.add('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" '
                 'stroke-width="%.2f" stroke-linecap="%s"/>' % (x1, y1, x2, y2, col, w, cap))

    def rect(self, x, y, w, h, fill='none', stroke=INK, sw=0.3, rx=0):
        self.add('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" rx="%.2f" '
                 'fill="%s" stroke="%s" stroke-width="%.2f"/>' % (x, y, w, h, rx, fill, stroke, sw))

    def path(self, d, fill='none', stroke=INK, sw=0.3, extra=''):
        self.add('<path d="%s" fill="%s" stroke="%s" stroke-width="%.2f"%s/>'
                 % (d, fill, stroke, sw, extra))

    def ellipse(self, cx, cy, rx, ry, fill=INK, stroke='none', sw=0.0, tilt=HEAD_TILT):
        self.add('<ellipse cx="0" cy="0" rx="%.3f" ry="%.3f" fill="%s" stroke="%s" '
                 'stroke-width="%.2f" transform="translate(%.3f %.3f) rotate(%d)"/>'
                 % (rx, ry, fill, stroke, sw, cx, cy, tilt))

    def svg(self):
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" width="%gmm" height="%gmm" '
                'viewBox="0 0 %g %g">\n<rect width="%g" height="%g" fill="#FFFFFF"/>\n%s\n</svg>\n'
                % (PAGE_W, PAGE_H, PAGE_W, PAGE_H, PAGE_W, PAGE_H, '\n'.join(self.out)))


# ----------------------------------------------------------------- notation
def notehead(c, x, y, dur):
    """Filled for q/e, open for h/w; whole notes are wider."""
    rx, ry = HEAD_RX, HEAD_RY
    if dur == 'w':
        rx, ry = rx * WHOLE_SCALE, ry * WHOLE_SCALE
    if dur in ('q', 'e'):
        c.ellipse(x, y, rx, ry)
    else:
        c.ellipse(x, y, rx, ry, fill=INK)
        c.ellipse(x, y, rx * 0.52, ry * 0.46, fill='#FFFFFF')


def draw_note(c, x, y, finger, dur, up=True, show_finger=True):
    notehead(c, x, y, dur)
    if dur != 'w':
        sx = x + (HEAD_RX * 0.92 if up else -HEAD_RX * 0.92)
        sy2 = y - STEM_LEN if up else y + STEM_LEN
        c.line(sx, y, sx, sy2, w=STEM_W, cap='round')
        fy = sy2 - FINGER_DY if up else sy2 + FINGER_DY + 2.6
        fx = sx
    else:
        fy = y - HEAD_RY * WHOLE_SCALE - FINGER_DY if up else y + HEAD_RY * WHOLE_SCALE + FINGER_DY + 2.6
        fx = x
    if show_finger:
        c.text(fx, fy, finger, size=4.0, weight='bold', anchor='middle')


def arrow(c, x1, y1, x2, y2):
    """Thin curved hand-change arrow."""
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    bend = 2.2 if y2 < y1 else -2.2
    c.path('M %.2f %.2f Q %.2f %.2f %.2f %.2f' % (x1, y1, mx + bend, my, x2, y2),
           sw=0.28)
    import math
    a = math.atan2(y2 - my, x2 - (mx + bend))
    for s in (2.6, -2.6):
        c.line(x2, y2,
               x2 - 2.0 * math.cos(a + math.radians(s * 9)),
               y2 - 2.0 * math.sin(a + math.radians(s * 9)), w=0.28)


def repeat_sign(c, x, y_top, y_bot):
    c.line(x, y_top, x, y_bot, w=1.7)
    c.line(x - 2.1, y_top, x - 2.1, y_bot, w=0.45)
    mid = (y_top + y_bot) / 2
    for dy in (-2.4, 2.4):
        c.add('<circle cx="%.2f" cy="%.2f" r="0.72" fill="%s"/>' % (x - 4.4, mid + dy, INK))


def parse_seq(seq):
    """'<finger><duration>' with an optional trailing +n / -n position shift."""
    out = []
    for tok in seq.split():
        shift = 0
        for sgn in ('+', '-'):
            if sgn in tok[2:]:
                i = tok.index(sgn, 2)
                shift = int(tok[i:])
                tok = tok[:i]
        out.append((tok[0], tok[1], shift))
    return out


def pitch_offset(hand_up, finger, shift=0, ps=None):
    """Vertical offset in mm; up the page is negative y."""
    step = (int(finger) - 2) if hand_up else -(int(finger) - 2)
    return -(step + shift) * (ps if ps else PITCH_STEP)


def lyric_tokens(s):
    return s.split() if s else []


# ----------------------------------------------------------------- page bits
def mini_keyboard(c, x, y, w, lh=(), rh=(), brackets=True):
    """5 white keys + the 2/3 black-key groups, finger numbers on the blacks."""
    kw = w / 5.0
    kh = kw * 2.35
    bw, bh = kw * 0.58, kh * 0.62
    for i in range(5):
        c.rect(x + i * kw, y, kw, kh, fill='#FFFFFF', sw=0.32)
    nums = list(lh) + list(rh)
    for j in range(3):
        bx = x + (j + 1) * kw - bw / 2
        c.rect(bx, y, bw, bh, fill=INK, stroke=INK, sw=0.32)
    # the book shows a 2-group then a 3-group; here: blacks at 1,2 and 3,4,5
    labels = nums
    xs = [x + (j + 1) * kw for j in range(3)]
    for k, lab in enumerate(labels[:3]):
        c.text(xs[k], y + bh * 0.72, lab, size=2.6, weight='bold',
               anchor='middle', fill='#FFFFFF')
    if brackets:
        c.line(x, y + kh + 1.2, x + 2 * kw, y + kh + 1.2, w=0.3)
        c.text(x + kw, y + kh + 4.0, 'L.H.', size=2.9, weight='bold', anchor='middle')
    return kh


def key_groups(c, x, y, w, groups, labels, brackets):
    """A keyboard fragment: `groups` is the run of black-key groups left to
    right, e.g. [3, 2, 3]; `labels` gives the finger printed on each black key
    of each group; `brackets` marks which group each hand sits on."""
    whites = sum(g + 1 for g in groups)          # 3-group spans 4 whites, 2-group 3
    kw = w / float(whites)
    kh = kw * 2.45
    bw, bh = kw * 0.58, kh * 0.63
    for i in range(whites):
        c.rect(x + i * kw, y, kw, kh, fill='#FFFFFF', sw=0.32)
    gx = []                                       # white-key index each group starts at
    wi = 0
    for g in groups:
        gx.append(wi)
        for j in range(g):
            bx = x + (wi + j + 1) * kw - bw / 2
            c.rect(bx, y, bw, bh, fill=INK, stroke=INK, sw=0.32)
        wi += g + 1
    for gi, labs in enumerate(labels):
        for j, lab in enumerate(labs or []):
            cx = x + (gx[gi] + j + 1) * kw
            c.text(cx, y + bh * 0.72, lab, size=kw * 0.42, weight='bold',
                   anchor='middle', fill='#FFFFFF')
    for br in brackets or []:
        gi = br['group']
        x0 = x + (gx[gi] + 0.55) * kw
        x1 = x + (gx[gi] + groups[gi] + 0.45) * kw
        yb = y + kh + 1.3
        c.line(x0, yb, x1, yb, w=0.35)
        c.line(x0, yb, x0, yb - 1.1, w=0.35)
        c.line(x1, yb, x1, yb - 1.1, w=0.35)
        c.text((x0 + x1) / 2, yb + 4.0, br['text'], size=3.2, weight='bold',
               anchor='middle')
    return kh


def final_barline(c, x, y_top, y_bot):
    c.line(x - 2.1, y_top, x - 2.1, y_bot, w=0.45)
    c.line(x, y_top, x, y_bot, w=1.7)


def two_three_keyboard(c, x, y, w, lh, rh):
    """The book's 'Find the Keys' diagram: a 2-group and a 3-group."""
    kw = w / 7.0
    kh = kw * 2.5
    bw, bh = kw * 0.58, kh * 0.63
    for i in range(7):
        c.rect(x + i * kw, y, kw, kh, fill='#FFFFFF', sw=0.32)
    blacks = [1, 2, 4, 5, 6]          # positions of the 2-group then 3-group
    bxs = []
    for wi in blacks:
        bx = x + wi * kw - bw / 2
        c.rect(bx, y, bw, bh, fill=INK, stroke=INK, sw=0.32)
        bxs.append(bx + bw / 2)
    labels = list(lh) + list(rh)
    for k, lab in enumerate(labels[:5]):
        c.text(bxs[k], y + bh * 0.70, lab, size=2.7, weight='bold',
               anchor='middle', fill='#FFFFFF')
    c.line(x + 0.5 * kw, y + kh + 1.0, x + 2.5 * kw, y + kh + 1.0, w=0.3)
    c.line(x + 0.5 * kw, y + kh + 1.0, x + 0.5 * kw, y + kh + 0.964, w=0.3)
    c.text(x + 1.5 * kw, y + kh + 4.2, 'L.H.', size=3.0, weight='bold', anchor='middle')
    c.line(x + 3.5 * kw, y + kh + 1.0, x + 6.5 * kw, y + kh + 1.0, w=0.3)
    c.text(x + 5.0 * kw, y + kh + 4.2, 'R.H.', size=3.0, weight='bold', anchor='middle')
    return kh


# ----------------------------------------------------------------- renderer
def render(doc):
    c = Canvas()
    m = doc.get('margin', 16.0)

    # --- practice steps block
    ps = doc.get('practice_steps')
    if ps:
        y = doc.get('practice_steps_y', 15)
        c.text(m, y, 'Practice Steps', size=4.2, weight='bold')
        for i, step in enumerate(ps):
            c.text(m + 4, y + 5.6 + i * 4.4, step, size=3.4)

    if doc.get('title'):
        tx = doc.get('title_x')
        c.text(tx if tx is not None else m, doc.get('title_y', 72), doc['title'],
               size=9.5, weight='bold',
               anchor='middle' if tx is not None else 'start')

    intro = doc.get('intro')
    if intro:
        for i, ln in enumerate(intro['lines']):
            c.text(m, intro['y'] + i * 4.4, ln, size=3.5)

    terms = doc.get('terms')
    if terms:
        y = terms['y']
        for col in terms.get('cols', []):
            c.text(m + col['x'], y, col['head'], size=3.7, weight='bold')
            c.text(m + col['x'], y + 8.0, col['sym'], size=5.2,
                   weight='bold', style='italic')
            c.text(m + col['x'] + 5.5, y + 8.0, col['gloss'], size=4.4,
                   style='italic')

    bl = doc.get('bullet')
    if bl:
        c.text(m + 2, bl['y'], '\u2022  ' + bl['text'], size=3.6)

    # --- find the keys
    fk = doc.get('find_keys')
    if fk:
        fx = doc.get('find_keys_x', m + 22)
        fy = doc.get('find_keys_y', 40)
        fw = doc.get('find_keys_w', 28)
        c.text(fx + fw / 2, fy - 2.5, 'Find the Keys', size=4.0, weight='bold',
               anchor='middle')
        if 'groups' in fk:
            key_groups(c, fx, fy, fw, fk['groups'], fk.get('labels', []),
                       fk.get('brackets', []))
        else:
            two_three_keyboard(c, fx, fy, fw, fk.get('lh', []), fk.get('rh', []))

    # --- eye check
    ec = doc.get('eye_check')
    if ec:
        y = doc.get('eye_check_y', 74)
        c.text(m, y, 'Eye Check:', size=3.7, weight='bold', spacing=0.2)
        for i, ln in enumerate(ec):
            c.text(m + 24, y + i * 4.3, ln, size=3.4)

    # --- the music -------------------------------------------------------
    ps = doc.get('pitch_step', PITCH_STEP)

    systems = doc.get('systems')
    if not systems:                      # old flat form: one system, hands alternate
        systems = [{'y': doc.get('system_y', 100.0),
                    'groups': doc.get('notes', []),
                    'lyrics': doc.get('lyrics', []),
                    'lyric_pos': 'below',
                    'count': doc.get('count'),
                    'repeat': doc.get('repeat'),
                    'inline_hands': True}]

    for sysdef in systems:
        y0 = sysdef['y']
        groups = sysdef['groups']
        lyr = [lyric_tokens(t) for t in sysdef.get('lyrics', [])]
        above = sysdef.get('lyric_pos', 'below') == 'above'
        primary = sysdef.get('primary', groups[0].get('hand', 'RH')).upper().replace('.', '')
        x = sysdef.get('x', doc.get('system_x', m + 12))
        li = 0
        prev_end = None
        prev_hand = None

        for g in groups:
            hand = g.get('hand', primary).upper().replace('.', '')
            up = hand == 'RH'
            y = y0 if hand == primary else y0 + HAND_GAP
            x += g.get('gap', 0)
            if hand != prev_hand:
                c.text(x - 3.0, y + 1.4, 'R.H.' if up else 'L.H.', size=4.6,
                       weight='bold', anchor='end')
            if g.get('dyn'):
                c.text(x - 7.5, y + 6.2, g['dyn'], size=5.8, weight='bold',
                       style='italic', anchor='middle')
            if prev_end and prev_hand and hand != prev_hand and sysdef.get('inline_hands'):
                arrow(c, prev_end[0] + 2.0,
                      prev_end[1] + (3.0 if prev_hand == 'RH' else -3.0),
                      x - 1.0, y + (3.5 if up else -3.5))
            g['_first'] = li
            last_f = None
            for fin, dur, shift in parse_seq(g['seq']):
                ny = y + pitch_offset(up, fin, shift, ps)
                draw_note(c, x, ny, fin, dur, up=up, show_finger=(fin != last_f))
                last_f = fin
                disp = g.get('lyric_display')
                for k, line in enumerate(lyr):
                    if li >= len(line):
                        continue
                    tok = line[li]
                    ly = (y0 - LYRIC_ABOVE - (len(lyr) - 1 - k) * LYRIC_LEAD
                          if above else y0 + LYRIC_DY + k * LYRIC_LEAD)
                    if disp and k == 0:
                        cols = g.get('lyric_colors', [])
                        col = cols[(li - g['_first']) % len(cols)] if cols else INK
                        t = tok[:-1] if tok.endswith('-') else tok
                        c.text(x, ly + 1.4, t, size=7.5, weight='bold',
                               style='italic', anchor='middle', fill=col)
                        if tok.endswith('-'):
                            c.text(x + ADV[dur] / 2, ly, '-', size=3.4, anchor='middle')
                        continue
                    if tok.endswith('-') and len(tok) > 1:
                        tok = tok[:-1]
                        c.text(x + ADV[dur] / 2, ly, '-', size=3.4, anchor='middle')
                    c.text(x, ly, tok, size=3.4, anchor='middle')
                li += 1
                prev_end = (x, ny)
                x += ADV[dur]
            prev_hand = hand

        y_lo = y0 + (HAND_GAP if any(g.get('hand', primary).upper().replace('.', '') != primary
                                     for g in groups) else 0)
        if sysdef.get('count'):
            c.text(x + 1.0, y0 + LYRIC_DY, sysdef['count'], size=3.4)
            x += 20
        if sysdef.get('repeat'):
            repeat_sign(c, x + 3, y0 - 10, y_lo + 10)
        elif sysdef.get('barline'):
            final_barline(c, x + 3, y0 - 7, y_lo + 7)

    # --- callout box
    cb = doc.get('callout')
    if cb:
        bx, by = cb.get('x', 180), cb.get('y', 150)
        bw, bh = 52, 6 + 4.6 * len(cb['lines'])
        c.rect(bx, by, bw, bh, fill='#FFFFFF', stroke='#2B4C7E', sw=0.4)
        c.text(bx + bw / 2, by + 5.4, cb['title'], size=3.8, weight='bold', anchor='middle')
        for i, ln in enumerate(cb['lines']):
            c.text(bx + bw / 2, by + 10.4 + i * 4.4, ln, size=3.5, anchor='middle')

    dv = doc.get('discovery')
    if dv:
        y = dv['y']
        c.setfill = None
        c.path('M %.2f %.2f L %.2f %.2f L %.2f %.2f Z'
               % (m, y + 1, m + 8.5, y + 1, m + 4.25, y - 6.5),
               fill='#4E7CA8', stroke='none')
        c.text(m - 0.5, y - 8.0, 'D I S C O V E R Y', size=3.0, weight='bold',
               fill='#4E7CA8')
        tx = m + 13
        c.text(tx, y - 1.0, dv['before'], size=3.6)
        gx = tx + len(dv['before']) * 1.62 + 5
        for tok in dv['rhythm'].split():
            draw_note(c, gx, y - 1.8, '', tok, up=True, show_finger=False)
            gx += 5.6 if tok == 'q' else 6.4
        c.text(gx + 1.0, y - 1.0, dv['after'], size=3.6)

    if doc.get('staff'):
        for sd in ([doc['staff']] if isinstance(doc['staff'], dict) else doc['staff']):
            draw_staff(c, sd)

    if doc.get('page'):
        c.text(m, PAGE_H - 8, str(doc['page']), size=3.2, fill='#555555')

    return c.svg()


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f:
        doc = yaml.safe_load(f)
    with open(dst, 'w') as f:
        f.write(render(doc))
    print('wrote', dst)
    want_png = '--png' in sys.argv
    want_pdf = '--pdf' in sys.argv
    if want_png or want_pdf:
        import cairosvg
        base = dst[:-4] if dst.endswith('.svg') else dst
        if want_png:
            cairosvg.svg2png(url=dst, write_to=base + '.png', output_width=2200)
            print('wrote', base + '.png')
        if want_pdf:
            cairosvg.svg2pdf(url=dst, write_to=base + '.pdf')
            print('wrote', base + '.pdf')




# ===================================================================== STAFF
# Conventional notation, for the teacher-duet system at the foot of a page.
MUSIC_FONT = 'FreeSerif, DejaVu Sans, serif'
LETTERS = {'C': 0, 'D': 1, 'E': 2, 'F': 3, 'G': 4, 'A': 5, 'B': 6}
# bass clef: top line is A3
TOP_IDX = 3 * 7 + LETTERS['A']
# flat order, as staff positions below the top line (bass clef)
FLAT_POS = [6, 3, 7, 4, 8, 5]


def pitch_pos(tok):
    """'Bb3' -> staff position; 0 = top line, +1 per half space downward."""
    letter = tok[0].upper()
    i = 1
    while i < len(tok) and tok[i] in 'b#':
        i += 1
    octv = int(tok[i:])
    return TOP_IDX - (octv * 7 + LETTERS[letter])


def parse_staff_seq(seq):
    """'Bb3q Bb3q | Db4h' -> [(pitch, dur, bar_after)]"""
    out = []
    for tok in seq.split():
        if tok == '|':
            if out:
                out[-1] = out[-1][:2] + (True,)
            continue
        out.append((tok[:-1], tok[-1], False))
    return out


def draw_staff(c, sd):
    x0 = sd['x']
    y0 = sd['y']                      # y of the TOP staff line
    sp = sd.get('space', 2.4)         # one staff space, mm
    width = sd['width']
    half = sp / 2.0

    def ypos(p):
        return y0 + p * half

    for k in range(5):
        c.line(x0, y0 + k * sp, x0 + width, y0 + k * sp, w=0.22)

    xc = x0 + 2.0
    c.text(xc, y0 + 3 * sp, '\U0001D122', size=sp * 4.6, family=MUSIC_FONT)
    xc += sp * 3.4

    for i in range(sd.get('flats', 0)):
        c.text(xc + i * sp * 0.78, ypos(FLAT_POS[i]) + sp * 0.34, '\u266D',
               size=sp * 2.5, family=MUSIC_FONT)
    xc += sd.get('flats', 0) * sp * 0.78 + sp * 0.8

    if sd.get('time'):
        a, b = sd['time'].split('/')
        c.text(xc + sp, y0 + sp * 1.42, a, size=sp * 2.3, weight='bold', anchor='middle')
        c.text(xc + sp, y0 + sp * 3.42, b, size=sp * 2.3, weight='bold', anchor='middle')
        xc += sp * 2.6

    music_x0 = xc + sp
    voices = sd['voices']
    BEATS = {'q': 1.0, 'h': 2.0, 'w': 4.0, 'e': 0.5}
    total = max(sum(BEATS[d] for _, d, _ in parse_staff_seq(v['seq'])) for v in voices)
    adv = (x0 + width - music_x0 - sp) / float(total)      # mm per beat

    bars = set()
    for v in voices:
        up = v.get('stem', 'up') == 'up'
        notes = parse_staff_seq(v['seq'])
        pts = []
        beat = 0.0
        for i, (pit, dur, bar) in enumerate(notes):
            x = music_x0 + beat * adv
            beat += BEATS[dur]
            p = pitch_pos(pit)
            y = ypos(p)
            pts.append((x, y))
            if bar:
                bars.add(music_x0 + beat * adv - adv * 0.35)
            # ledger lines
            k = -2
            while k >= p:
                if k % 2 == 0:
                    c.line(x - sp * 0.95, ypos(k), x + sp * 0.95, ypos(k), w=0.22)
                k -= 1
            k = 10
            while k <= p:
                if k % 2 == 0:
                    c.line(x - sp * 0.95, ypos(k), x + sp * 0.95, ypos(k), w=0.22)
                k += 1
            rx, ry = sp * 0.58, sp * 0.42
            if dur == 'q':
                c.ellipse(x, y, rx, ry, tilt=-20)
            else:
                c.ellipse(x, y, rx, ry, fill=INK, tilt=-20)
                c.ellipse(x, y, rx * 0.5, ry * 0.44, fill='#FFFFFF', tilt=-20)
            if dur != 'w':
                sx = x + (rx * 0.92 if up else -rx * 0.92)
                sy = y - sp * 2.8 if up else y + sp * 2.8
                c.line(sx, y, sx, sy, w=0.28)
            if v.get('fingers', {}).get(str(i)):
                fy = y - sp * 3.9 if up else y + sp * 3.9
                c.text(x, fy, v['fingers'][str(i)], size=sp * 1.3, weight='bold',
                       anchor='middle')
        for sl in v.get('slurs', []):
            (ax, ay), (bx, by) = pts[sl[0]], pts[sl[1]]
            top = min(ay, by) - sp * (4.2 if up else -4.2)
            c.path('M %.2f %.2f Q %.2f %.2f %.2f %.2f'
                   % (ax, ay - sp * 1.2, (ax + bx) / 2, top, bx, by - sp * 1.2), sw=0.3)

    for bx in sorted(bars):
        c.line(bx, y0, bx, y0 + 4 * sp, w=0.25)
    c.line(x0 + width - 1.6, y0, x0 + width - 1.6, y0 + 4 * sp, w=0.25)
    c.line(x0 + width, y0, x0 + width, y0 + 4 * sp, w=0.9)

    for lb in sd.get('labels', []):
        c.text(lb['x'], lb['y'], lb['text'], size=lb.get('size', 3.4),
               weight=lb.get('weight', 'bold'),
               style=lb.get('style', 'normal'))


if __name__ == '__main__':
    main()
