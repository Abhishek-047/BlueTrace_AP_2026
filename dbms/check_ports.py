#!/usr/bin/env python3
"""Checks the Python ports in bluetrace_logic.py against the app itself.

The same inputs go through the app's own JavaScript (blue-grid/index.html, run
in headless Chromium by check_ports_browser.js) and through the ports, and the
results must agree:

    python3 dbms/check_ports.py [--plots 5000] [--grid 10000] [--seed 7]

  PARAMS, UNCERTAINTY_M  after the app's reconcileParams(corpus)          exact
  decodeUpa()            all 256 byte codes                               what is stored exact; the raw
                                                                          value within 1 ulp of 10^x
  sig2()                 every decoded upstream area, numbers that sit
                         exactly on a tie at two significant figures,
                         random numbers                                    exact
  fnv1a()                edge cases, non-ASCII and random strings         exact
  readSite()             plots sampled the way generate_workload.py
                         samples them, of the sizes it uses                exact, except encroached
  traceDownstream()      the same plots, step by step                     exact; step length to 0.05 m
  decide()               the measurements read at those plots, and a grid
                         of values on every threshold, under both rules   verdict, codes and wording exact

decodeUpa() raises 10 to a power: V8's Math.pow and the C library's pow can
differ in the last bit, which no stored value (two significant figures) and no
decision (the 10 km2 trunk test) can see. encroached is read from a mask the
browser downscales from 2,048 to 1,024 pixels with its own smoothing, which
Pillow's bilinear resize only approximates; for it the report gives the largest
difference and the number of plots whose verdict it changes.

Needs node, and Playwright with Chromium. Exit status 0 when everything agrees.
"""
import argparse
import json
import math
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction

from bluetrace_logic import (APP, CITIES, D8_NAME, MERIT_DEG, PARAMS, RULESETS, UNCERTAINTY_M,
                             Layers, decide_, decode_upa, fnv1a, sig2)
from generate_workload import CITY, PLOT_M, centroids, weighted

HERE = pathlib.Path(__file__).resolve().parent
SITE_FIELDS = ('distPresent', 'distHistoric', 'distDrain', 'lastWaterYear', 'occurrence', 'handM')


class Report:
    def __init__(self):
        self.failed = False

    def line(self, what, cases, problems, note=''):
        """One line per comparison; problems holds (input, app, Python) examples."""
        status = 'ok' if not problems else '%d DIFFER' % len(problems)
        print('  %-44s %7s   %s%s' % (what, '{:,}'.format(cases), status, ('   ' + note) if note else ''))
        for p in problems[:3]:
            print('      e.g. %s' % (p,))
        if problems:
            self.failed = True


# ------------------------------------------------------------------ inputs
def exact_ties():
    """Numbers whose value sits exactly halfway between two numbers of two
    significant figures (0.125, 12.5, 1250, ...): the only inputs on which two
    rounding rules can disagree."""
    out = []
    for mant in range(105, 1000, 10):          # three digits ending in 5
        for e in range(-8, 9):
            v = Fraction(mant) * Fraction(10) ** e / 100
            f = float(v)
            if Fraction(f) == v:
                out += [f, -f]
    return out


def random_strings(rng):
    """Edge cases plus random strings: ASCII, the rest of the Basic Multilingual
    Plane (Kannada, the rupee sign, ...) and characters beyond it (emoji)."""
    fixed = ['', 'a', 'abc', '0123456789' * 50, 'The plot lies 35 m from the present water edge.',
             'café', 'ಬೆಂಗಳೂರು', '₹ 20 m', 'Ω', '\u00ff', '\u0100', '\ufffd',
             '\U0001F600', 'x\U0001F30Ay', '\U00010000', '\U0010FFFF']
    bmp = [(0x20, 0x7e), (0xa0, 0xd7ff), (0xe000, 0xfffd)]
    out = list(fixed)
    for _ in range(300):
        out.append(''.join(chr(rng.randint(*rng.choice(bmp))) for _ in range(rng.randint(1, 60))))
    for _ in range(50):
        out.append(''.join(chr(rng.randint(0x10000, 0x10ffff)) if rng.random() < 0.3
                           else chr(rng.randint(0x20, 0x7e)) for _ in range(rng.randint(1, 30))))
    return out


def random_numbers(rng):
    nums = [decode_upa(c) for c in range(256)] + exact_ties()
    for _ in range(3000):
        nums.append(rng.choice((1, -1)) * 10 ** rng.uniform(-6, 6))
    nums += [float(rng.randint(0, 5000)) for _ in range(500)]
    return nums


def plots(rng, n):
    """Where generate_workload.py puts plot checks: 60% anywhere in the study
    area, 40% within about 100 m of a flagged building; its plot sizes and its
    55/45 split between the two rules."""
    b, near, out = CITIES[CITY]['bounds'], centroids(), []
    for _ in range(n):
        if rng.random() < 0.6:
            lon, lat = rng.uniform(b[0], b[2]), rng.uniform(b[1], b[3])
        else:
            clon, clat = near[rng.randrange(len(near))]
            lon, lat = clon + rng.uniform(-0.0009, 0.0009), clat + rng.uniform(-0.0009, 0.0009)
        out.append([round(lon, 6), round(lat, 6), weighted(rng, PLOT_M),
                    'state' if rng.random() < 0.55 else 'ngt'])
    return out


def decide_grid(rng, n):
    """Measurement records on and around every threshold decide() tests. Values
    that are printed in the reasons are whole numbers, as the rasters give them."""
    U = UNCERTAINTY_M
    lake = sorted({r['lakeM'] for r in RULESETS.values()})
    drain = sorted({r['drainM'] for r in RULESETS.values()})
    dp_vals = [None, 0, 1, 254, 255] + [max(0, v + d) for v in lake for d in (-U - 1, -U, -U + 1, -1, 0, 1, U - 1, U, U + 1)]
    dd_vals = [None, 0, 1, 10, 11, 254, 255] + [max(0, v + d) for v in drain for d in (-U - 1, -U, -1, 0, U - 1, U, U + 1)]
    hand = int(PARAMS['handDeepM'])
    hand_vals = [None, 0, 1, 2, hand, hand + 1, 5, 254, 255]
    out, seen = [], set()
    while len(out) < n:
        dp = rng.choice(dp_vals)
        if dp is not None and rng.random() < 0.5:
            dh = max(0, dp - rng.choice((29, 30, 31)))          # the shoreline-retreat test
        else:
            dh = rng.choice([None, 0, 1, 20, 50, 74, 75, 76, 255])
        m = dict(distPresent=dp, distHistoric=dh, distDrain=rng.choice(dd_vals),
                 lastWaterYear=rng.choice([None, 1984, 1999, 2017]),
                 occurrence=rng.choice([None, 0, 1, 16, 49, 50, 51, 100]),
                 handM=rng.choice(hand_vals),
                 encroached=rng.choice([0, 0.05, 0.1, 0.1000001, 0.2, 1.0]))
        rule = rng.choice(sorted(RULESETS))
        key = (tuple(m.values()), rule)
        if key not in seen:
            seen.add(key)
            out.append([m, rule])
    return out


# ------------------------------------------------------------------ the check
def run_app(inputs, node):
    with tempfile.TemporaryDirectory() as tmp:
        fin, fout = pathlib.Path(tmp, 'in.json'), pathlib.Path(tmp, 'out.json')
        fin.write_text(json.dumps(inputs))
        r = subprocess.run([node, str(HERE / 'check_ports_browser.js'), str(APP), str(fin), str(fout)])
        if r.returncode != 0:
            sys.exit('check_ports_browser.js failed (exit %d); it needs node and Playwright with Chromium'
                     % r.returncode)
        return json.loads(fout.read_text())


def same_number(a, b):
    return (a is None and b is None) or (a is not None and b is not None and float(a) == float(b))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--plots', type=int, default=5000, help='plots to read and trace (default 5000)')
    ap.add_argument('--grid', type=int, default=10000, help='measurement records on the thresholds (default 10000)')
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--node', default=shutil.which('node') or 'node')
    args = ap.parse_args()
    if args.plots < 1 or args.grid < 1:
        ap.error('--plots and --grid must be at least 1')

    rng = random.Random(args.seed)
    layers = Layers(CITY)
    pl = plots(rng, args.plots)
    py_sites = [layers.read_site(lon, lat, plot_m) for lon, lat, plot_m, _ in pl]
    site_records = [{k: s[k] for k in SITE_FIELDS + ('encroached',)} for s in py_sites]
    grid = decide_grid(rng, args.grid)
    inputs = dict(city=CITY, upaCodes=list(range(256)), numbers=random_numbers(rng),
                  strings=random_strings(rng), plots=pl,
                  decide=[[m, rule] for m, (_, _, _, rule) in zip(site_records, pl)] + grid)

    print('Python ports (dbms/bluetrace_logic.py) against the app (blue-grid/index.html) in headless Chromium;')
    print('examples of a difference are shown as (input, app, Python)')
    app = run_app(inputs, args.node)
    rep = Report()
    if app['pageErrors']:
        print('  note: the app reported %d error(s) while starting without its map: %s'
              % (len(app['pageErrors']), app['pageErrors'][0]))

    # parameters
    bad = [(k, v, PARAMS.get(k)) for k, v in app['params'].items() if not same_number(v, PARAMS.get(k))]
    if app['uncertaintyM'] != UNCERTAINTY_M:
        bad.append(('UNCERTAINTY_M', app['uncertaintyM'], UNCERTAINTY_M))
    rep.line('PARAMS + UNCERTAINTY_M after reconcileParams()', len(app['params']) + 1, bad)

    # decodeUpa, sig2, fnv1a
    bad, raw_same, gap, trunk = [], 0, 0.0, PARAMS['trunkUpaKm2']
    for c, a in zip(inputs['upaCodes'], app['decodeUpa']):
        py = decode_upa(c)
        raw_same += a == py
        ulps = abs((a + 1) - (py + 1)) / math.ulp(py + 1)      # in units of the last place of 10^x
        gap = max(gap, ulps)
        if sig2(a) != sig2(py) or (a > trunk) != (py > trunk) or ulps > 1:
            bad.append((c, a, py))
    rep.line('decodeUpa(), all 256 codes', 256, bad,
             'raw value bit-identical for %d codes, largest gap %g ulp' % (raw_same, gap))
    bad = [(x, a, sig2(x)) for x, a in zip(inputs['numbers'], app['sig2']) if not same_number(a, sig2(x))]
    rep.line('sig2()', len(inputs['numbers']), bad, '{:,} of them exact ties'.format(len(exact_ties())))
    bad = [(s[:24], a, fnv1a(s)) for s, a in zip(inputs['strings'], app['fnv1a']) if a != fnv1a(s)]
    rep.line('fnv1a()', len(inputs['strings']), bad)

    # readSite
    bad, enc_max, enc_cross = [], 0.0, 0
    for p, py, js in zip(pl, py_sites, app['sites']):
        diff = [(k, js[k], py[k]) for k in SITE_FIELDS if not same_number(js[k], py[k])]
        if diff:
            bad.append((p[:3], diff))
        enc_max = max(enc_max, abs(js['encroached'] - py['encroached']))
        enc_cross += (js['encroached'] > .1) != (py['encroached'] > .1)
    rep.line('readSite(), 6 measured fields', len(pl), bad)
    changed = []
    for p, py, js, (d, codes) in zip(pl, site_records, app['sites'], app['siteVerdicts']):
        verdict, found = decide_(py, RULESETS[p[3]])
        if (verdict, [c for c, _ in found]) != (d, codes):
            changed.append((p, 'encroached app %.3f, Python %.3f' % (js['encroached'], py['encroached']),
                            (d, codes), (verdict, [c for c, _ in found])))
    print('  %-44s %7s   largest difference %.3f, %d plot(s) on opposite sides of 0.1, %d verdict(s) change'
          % ('readSite(), encroached', '{:,}'.format(len(pl)), enc_max, enc_cross, len(changed)))
    for c in changed[:3]:
        print('      e.g. %s' % (c,))

    # traceDownstream
    bad, n_steps, len_max = [], 0, 0.0
    for p, js in zip(pl, app['traces']):
        steps, ended = layers.trace(p[0], p[1])
        n_steps += len(steps)
        js_steps = js['steps']
        if len(steps) != len(js_steps) or (steps and ended != js['endedBy']):
            bad.append((p[:2], (len(js_steps), js['endedBy']), (len(steps), ended)))
            continue
        for k, ((direction, length, upa, hand), (dlon, dlat, js_len, js_upa, js_hand)) \
                in enumerate(zip(steps, js_steps), start=1):
            js_dir = D8_NAME.get((round(dlon / MERIT_DEG), round(-dlat / MERIT_DEG)))
            len_max = max(len_max, abs(length - js_len))
            if (direction != js_dir or abs(length - js_len) > 0.05 + 1e-9
                    or (upa is not None and not same_number(upa, js_upa))
                    or (hand is not None and hand != js_hand)):
                bad.append((p[:2], 'step %d' % k, (js_dir, round(js_len, 3), js_upa, js_hand),
                            (direction, length, upa, hand)))
                break
    rep.line('traceDownstream()', len(pl), bad,
             '%s steps; step lengths within %.3f m' % ('{:,}'.format(n_steps), len_max))

    # decide
    bad = []
    for (m, rule), (d, codes, why) in zip(inputs['decide'], app['decisions']):
        verdict, found = decide_(m, RULESETS[rule])
        if (verdict, [c for c, _ in found]) != (d, codes) or ' '.join(t for _, t in found) != ' '.join(why):
            bad.append(((m, rule), (d, codes), (verdict, [c for c, _ in found])))
    rep.line('decide(), same measurements, wording included', len(inputs['decide']), bad,
             '%d read at the plots + %d on the thresholds' % (len(pl), len(grid)))
    print('All checks agree.' if not rep.failed else 'Some checks differ (above).')
    sys.exit(1 if rep.failed else 0)


if __name__ == '__main__':
    main()
