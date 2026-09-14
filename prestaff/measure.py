#!/usr/bin/env python3
"""
measure — read geometry off a photographed music page, so a recreation can be
checked against the original instead of eyeballed.

Two modes:

  measure.py heads  IMG x0,y0,x1,y1 [debug.png]
      Finds noteheads (morphological opening kills the thin stems, so heads
      survive as blobs). Prints x, y, w, h per head. Use for PRE-STAFF pages:
      finger-number notation where vertical position encodes pitch.

  measure.py staff  IMG x0,y0,x1,y1 [debug.png]
      Fits a 5-line comb per column band, so it tracks the curl of a
      photographed page, then reports each notehead's staff position
      (0 = top line, +1 per half space downward). Use for real staves.

ALWAYS de-tilt before drawing conclusions. A page photographed by hand drifts
several pixels per note; that drift reads exactly like a descending melody and
will fool you. Find a run of notes you KNOW are the same pitch (same finger,
same hand), measure the drift across them, and subtract it from everything.

The debug PNG marks what was detected. Look at it. A detector you have not
eyeballed is a detector that is lying to you.
"""
import sys
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage


def load(path, box):
    im = Image.open(path).convert('L').crop(box)
    a = np.array(im, float)
    bg = ndimage.uniform_filter(a, size=101)
    return im, np.clip(bg - a, 0, None)


def find_heads(d, hmin=12, hmax=46, wmin=16, wmax=85, thresh=30):
    """Noteheads as blobs. The (5, 11) opening removes stems but keeps heads."""
    mask = ndimage.binary_opening(d > thresh, np.ones((5, 11)))
    lab, _ = ndimage.label(mask)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab)):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        comp = (lab[sl] == i + 1)
        if not (hmin <= h <= hmax and wmin <= w <= wmax):
            continue
        if comp.sum() / float(h * w) < 0.45:
            continue
        cy, cx = ndimage.center_of_mass(comp)
        out.append((sl[1].start + cx, sl[0].start + cy, w, h))
    out.sort()
    return out


def fit_staff(d, step=20, lo=19.0, hi=23.0):
    """Best 5-line comb per column band. Returns (xs, top_ys, spacing)."""
    H, W = d.shape
    xs, tops, sps = [], [], []
    for x in range(0, W - step, step):
        prof = ndimage.uniform_filter1d(d[:, x:x + step].mean(axis=1), 3)
        best = (-1, 0, 0)
        for s in np.arange(lo, hi + 0.01, 0.25):
            for t in range(5, int(H - 4 * s - 5)):
                sc = sum(prof[int(round(t + k * s))] for k in range(5))
                if sc > best[0]:
                    best = (sc, t, s)
        xs.append(x + step / 2.0)
        tops.append(best[1])
        sps.append(best[2])
    return (np.array(xs), ndimage.median_filter(np.array(tops, float), 7),
            float(np.median(sps)))


def main():
    mode, img, box = sys.argv[1], sys.argv[2], [int(v) for v in sys.argv[3].split(',')]
    dbg = sys.argv[4] if len(sys.argv) > 4 else None
    im, d = load(img, box)
    vis = im.convert('RGB')
    dr = ImageDraw.Draw(vis)

    if mode == 'heads':
        heads = find_heads(d)
        print('heads: %d' % len(heads))
        for cx, cy, w, h in heads:
            dr.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(255, 0, 0))
            print('x=%7.1f y=%7.1f w=%d h=%d' % (cx, cy, w, h))
        if heads:
            hh = np.median([h for _, _, _, h in heads])
            print('median head height: %.1f px  '
                  '(one pitch step in this book is ~0.4-0.5 of that)' % hh)

    elif mode == 'staff':
        xs, tops, sp = fit_staff(d)
        print('staff space: %.2f px' % sp)
        for x in range(0, d.shape[1], 6):
            t = float(np.interp(x, xs, tops))
            for k in range(5):
                dr.point((x, int(t + k * sp)), fill=(255, 0, 0))
        for cx, cy, w, h in find_heads(d, hmin=12, hmax=34, wmin=16, wmax=40):
            t = float(np.interp(cx, xs, tops))
            dr.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=(0, 140, 255))
            print('x=%6.0f y=%6.1f pos=%6.2f' % (cx, cy, (cy - t) / (sp / 2.0)))
        print('pos 0 = top line. Bass clef: 0=A3, 2=F3, 4=D3, 6=B2, 8=G2.')
    else:
        print(__doc__)
        sys.exit(2)

    if dbg:
        vis.save(dbg)
        print('debug image:', dbg)


if __name__ == '__main__':
    main()
