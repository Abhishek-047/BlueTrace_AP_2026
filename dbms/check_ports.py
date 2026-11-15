#!/usr/bin/env python3
"""Checks the Python ports in bluetrace_logic.py against the app itself.

The same inputs go through the app's own JavaScript (blue-grid/index.html, run
in headless Chromium by check_ports_browser.js) and through the ports, and the
results must agree:

    python3 dbms/check_ports.py [--plots 5000] [--grid 10000] [--seed 7] [--db bluetrace_big]

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

With --db, the database the generator filled is checked against the app too:

  certificates           every one: the digest in its reference, PostgreSQL's fnv1a()
                         of the canonical record rebuilt from the database (R14's
                         record), Python's and the app's must be the same
  plot checks            a sample (--sample, default 2,000, plus one check for
                         every plot size, storage rule and buffer rule in the data)
                         run through the app's siteContext(): stored
                         measurements, upstream area, reasons and route against
                         what the app shows
  R8, R9                 run as written in 04_queries.sql for each sampled check:
                         the derived facts and the route totals against siteContext()

The app's nine example checks (load_app_data.py) carry illustrative measurements,
not raster readings, so they are left out of the sample. R9 sums step lengths
stored to 0.1 m and judges the ground its first step leaves by the plot's own
height above drainage, where traceDownstream() reads the 90 m cell the plot
falls in; the report counts both effects and fails on any other difference.

Needs node, and Playwright with Chromium. Exit status 0 when everything agrees.
"""
import argparse
import atexit
import json
import math
import pathlib
import random
import re
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
def json_lines(path):
    with open(path, encoding='utf-8') as f:
        for line in f:
            yield json.loads(line)


def run_app(inputs, node, tmp):
    """Runs check_ports_browser.js. The per-plot results come back one per line;
    the siteContext() results are read only as they are compared, so a whole
    database can be checked without holding them all in memory."""
    fin, fout = pathlib.Path(tmp, 'in.json'), pathlib.Path(tmp, 'out.json')
    fin.write_text(json.dumps(inputs))
    r = subprocess.run([node, str(HERE / 'check_ports_browser.js'), str(APP), str(fin), str(fout)])
    if r.returncode != 0:
        sys.exit('check_ports_browser.js failed (exit %d); it needs node and Playwright with Chromium'
                 % r.returncode)
    app = json.loads(fout.read_text())
    plots = list(json_lines(str(fout) + '.plots'))
    app['sites'], app['siteVerdicts'], app['traces'] = ([p[i] for p in plots] for i in range(3))
    app['contexts'] = json_lines(str(fout) + '.contexts')
    return app


def same_number(a, b):
    return (a is None and b is None) or (a is not None and b is not None and float(a) == float(b))


def same_value(a, b):
    """same_number() for numbers, plain equality for text and NULL."""
    if isinstance(a, str) or isinstance(b, str) or a is None or b is None:
        return a == b
    return float(a) == float(b)


def js_round(x):
    """Math.round(): halves go up."""
    return math.floor(x + 0.5)


# ------------------------------------------------------------------ the stored data (--db)
CANONICAL = """concat_ws('|', s.city_key, s.lat::text, s.lon::text, s.plot_m::text, r.rule_id,
                 r.lake_m::text, r.drain_m::text,
                 coalesce(s.dist_present::text, ''), coalesce(s.dist_historic::text, ''),
                 coalesce(s.dist_drain::text, ''), coalesce(s.last_water_year::text, ''),
                 coalesce(s.occurrence::text, ''), coalesce(s.hand_m::text, ''),
                 v.outcome,
                 to_char(ce.issued_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
                 ce.corpus_version, ce.generator,
                 coalesce((SELECT string_agg(cc.chunk_id, ',' ORDER BY cc.cite_order)
                           FROM certificate_chunk cc WHERE cc.cert_ref = ce.cert_ref), ''))"""

CERTIFICATES = """SELECT json_build_object('ref', ce.cert_ref, 'canon', c.canon, 'sql', fnv1a(c.canon))
FROM certificate ce
JOIN screening s   ON s.screening_id = ce.screening_id
JOIN buffer_rule r ON r.rule_id = s.rule_id
CROSS JOIN LATERAL (SELECT o.outcome FROM screening_reason sr
                    JOIN reason_code rc ON rc.reason_code = sr.reason_code
                    JOIN outcome o      ON o.outcome = rc.outcome
                    WHERE sr.screening_id = s.screening_id
                    ORDER BY o.rank DESC LIMIT 1) v
CROSS JOIN LATERAL (SELECT %s AS canon) c;""" % CANONICAL

STORED = """SELECT json_build_object(
    'id', s.screening_id, 'lon', s.lon, 'lat', s.lat, 'plotM', s.plot_m, 'rule', s.rule_id,
    'storage', s.storage_lpm2, 'encroached', s.encroached, 'upaKm2', s.upa_km2,
    'endedBy', s.route_ended_by,
    'm', json_build_object('distPresent', s.dist_present, 'distHistoric', s.dist_historic,
                           'distDrain', s.dist_drain, 'lastWaterYear', s.last_water_year,
                           'occurrence', s.occurrence, 'handM', s.hand_m),
    'reasons', (SELECT coalesce(json_agg(json_build_array(sr.reason_code, sr.reason_text)
                                         ORDER BY sr.reason_code), '[]')
                FROM screening_reason sr WHERE sr.screening_id = s.screening_id),
    'steps', (SELECT coalesce(json_agg(json_build_array(st.direction, st.step_length_m,
                                                        st.step_upa_km2, st.step_hand_m)
                                       ORDER BY st.step_no), '[]')
              FROM route_step st WHERE st.screening_id = s.screening_id))
FROM screening s WHERE s.screening_id IN (%s) ORDER BY s.screening_id;"""

# one check for every plot size, storage rule and buffer rule in the data
COMBINATIONS = """SELECT DISTINCT ON (plot_m, storage_lpm2, rule_id) screening_id
FROM screening WHERE dist_present IS NOT NULL AND screening_id > %d
ORDER BY plot_m, storage_lpm2, rule_id, screening_id;"""

# load_app_data.py loads the app's nine example checks first; their measurements
# are illustrative, not raster readings, so they cannot be compared with the rasters
EXAMPLES = 9

R8_FIELDS = [('plot_area_m2', 'plotAreaM2'), ('retreat_m', 'retreatM'), ('flow_class', 'flowClass'),
             ('hand_class', 'handClass'), ('runoff_displaced_m3', 'runoffDisplacedM3'),
             ('runoff_low_m3', 'runoffLoM3'), ('runoff_high_m3', 'runoffHiM3'),
             ('runoff_per_year_m3', 'runoffAnnualM3'), ('tanker_loads', 'tankerEquiv'),
             ('retention_required_m3', 'retentionRequiredM3'), ('rwh_required_m3', 'rwhRequiredM3'),
             ('shortfall_m3', 'retentionShortfallM3')]


def psql(db, sql):
    r = subprocess.run(['psql', '-d', db, '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1'],
                       input='SET search_path TO bluetrace;\n' + sql, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit('psql failed on %s: %s' % (db, r.stderr.strip()))
    return [line for line in r.stdout.splitlines() if line]


def query(qid, screening_id):
    """A query of 04_queries.sql exactly as time_queries.sh runs it, for another screening."""
    text, keep = [], False
    for line in (HERE / '04_queries.sql').read_text().splitlines():
        if line.startswith('-- @'):
            keep = line.split()[1] == '@' + qid
        elif keep and not line.startswith('--'):
            text.append(line)
    sql, n = re.subn(r'screening_id = \d+', 'screening_id = %d' % screening_id, '\n'.join(text))
    if n != 1:
        sys.exit('expected one screening id in %s, found %d' % (qid, n))
    return sql.strip().rstrip(';')


def load_stored(db, n, rng):
    """The certificates, a sample of the plot checks, and R8 / R9 for each sampled check."""
    certs = [json.loads(x) for x in psql(db, CERTIFICATES)]
    max_id = int(psql(db, 'SELECT max(screening_id) FROM screening;')[0])
    ids = {int(x) for x in psql(db, COMBINATIONS % EXAMPLES)}
    target = min(max_id - EXAMPLES, len(ids) + n)
    while len(ids) < target:
        ids.add(rng.randint(EXAMPLES + 1, max_id))
    ids = sorted(ids)
    if not ids:                                   # only the app's own data is loaded
        return certs, [], {}, {}
    rows = [json.loads(x) for x in psql(db, STORED % ', '.join(map(str, ids)))]
    script = []
    for i in ids:
        script.append("SELECT json_build_object('id', %d, 'row', row_to_json(q)) FROM (%s) q;" % (i, query('R8', i)))
        script.append("SELECT json_build_object('id', %d, 'rows', coalesce(json_agg(row_to_json(q) ORDER BY q.step_no), "
                      "'[]')) FROM (%s) q;" % (i, query('R9', i)))
    out = [json.loads(x) for x in psql(db, '\n'.join(script))]
    r8 = {o['id']: o['row'] for o in out if 'row' in o}
    r9 = {o['id']: o['rows'] for o in out if 'rows' in o}
    return certs, rows, r8, r9


def app_reasons(codes, why):
    """decide() can give one code several sentences; the stored reason joins them."""
    return sorted(zip(codes, why)) if len(codes) == len(why) else [(codes[0], ' '.join(why))]


def check_stored(db, rep, layers, certs, rows, r8, r9, cert_digests, contexts):
    print('Stored data in %s against the app (the %d illustrative example checks left out)' % (db, EXAMPLES))
    bad = [(c['ref'], c['sql'], fnv1a(c['canon']), a) for c, a in zip(certs, cert_digests)
           if not (c['ref'][-8:] == c['sql'] == fnv1a(c['canon']) == a)]
    ascii_only = all(ch < '\x80' for c in certs for ch in c['canon'])
    rep.line('certificates: stored = SQL = Python = app', len(certs), bad,
             'every canonical record ASCII' if ascii_only else 'NON-ASCII canonical records present')

    if not rows:
        print('  no generated plot checks to sample (load them with generate_workload.py)')
        return
    bad_m, bad_upa, bad_route, bad_r8, bad_r9, bad_reasons, by_encroached, enc_max = [], [], [], [], [], [], [], 0.0
    r9_rounding = r9_first = 0
    r9_gap = 0.0
    for row, ctx in zip(rows, contexts):
        sid, m = row['id'], ctx['m']
        m_diff = [(k, m[k], row['m'][k]) for k in SITE_FIELDS if not same_number(m[k], row['m'][k])]
        if m_diff:
            bad_m.append((sid, m_diff))
        if row['encroached'] is not None:
            enc_max = max(enc_max, abs(m['encroached'] - float(row['encroached'])))
        if not same_number(ctx['rec']['upaKm2'], row['upaKm2']):
            bad_upa.append((sid, ctx['rec']['upaKm2'], row['upaKm2']))
        # The reasons must be what decide_() makes of the generator's own reading
        # (Python, unrounded encroached). Where the app's reasons differ from them
        # with every measured field equal, the difference can only come from the
        # encroached resampling, which is reported, not counted as a failure.
        stored = sorted(map(tuple, row['reasons']))
        py = layers.read_site(float(row['lon']), float(row['lat']), row['plotM'])
        verdict, found = decide_({k: py[k] for k in SITE_FIELDS + ('encroached',)}, RULESETS[row['rule']])
        d, codes, why = ctx['verdict']
        if sorted(found) != stored:
            bad_reasons.append((sid, 'stored', [c for c, _ in stored], 'decide_()', [c for c, _ in found]))
        elif app_reasons(codes, why) != stored:
            (by_encroached if not m_diff else bad_reasons).append(
                (sid, 'encroached app %.3f, Python %.3f' % (m['encroached'], py['encroached']),
                 codes, [c for c, _ in stored]))
        steps, js = row['steps'], ctx['route']['steps']
        if len(steps) != len(js) or (steps and row['endedBy'] != ctx['route']['endedBy']):
            bad_route.append((sid, (len(js), ctx['route']['endedBy']), (len(steps), row['endedBy'])))
        else:
            for k, ((direction, length, upa, hand), (dlon, dlat, js_len, js_upa, js_hand)) \
                    in enumerate(zip(steps, js), start=1):
                js_dir = D8_NAME.get((round(dlon / MERIT_DEG), round(-dlat / MERIT_DEG)))
                if (direction != js_dir or abs(float(length) - js_len) > 0.05 + 1e-9
                        or (upa is not None and not same_number(upa, js_upa))
                        or (hand is not None and hand != js_hand)):
                    bad_route.append((sid, 'step %d' % k, (js_dir, js_len, js_upa, js_hand),
                                      (direction, length, upa, hand)))
                    break
        got = r8.get(sid)
        r8_diff = [(sql_k, ctx['rec'][app_k], got[sql_k]) for sql_k, app_k in R8_FIELDS
                   if not same_value(got[sql_k], ctx['rec'][app_k])] if got else ['no row']
        if r8_diff:
            bad_r8.append((sid, r8_diff))
        route = r9.get(sid, [])
        if route:
            total, low = float(route[-1]['metres_from_plot']), float(route[-1]['low_ground_m_so_far'])
            app_total, app_low = ctx['route']['lengthM'], ctx['route']['lowGroundM']
            # what R9 would give with the app's reading of the first cell
            first, hand_low = float(route[0]['step_length_m']), PARAMS['handLowM']
            plot_low = row['m']['handM'] is not None and row['m']['handM'] <= hand_low
            cell_low = ctx['route']['originHand'] is not None and ctx['route']['originHand'] <= hand_low
            low_cell = low - (first if plot_low else 0) + (first if cell_low else 0)
            # each stored length is within 0.05 m of the exact one, and the app
            # rounds the exact sum to whole metres
            tol = 0.5 + 0.05 * len(route) + 1e-9
            if abs(total - app_total) > tol or abs(low_cell - app_low) > tol:
                bad_r9.append((sid, (app_total, app_low), (total, low)))
            else:
                r9_rounding += js_round(total) != app_total or js_round(low_cell) != app_low
                r9_gap = max(r9_gap, abs(total - app_total), abs(low_cell - app_low))
                r9_first += plot_low != cell_low
    n = len(rows)
    rep.line('plot checks: measurements, 6 fields', n, bad_m)
    print('  %-44s %7s   largest difference %.3f (stored to 3 decimals)'
          % ('plot checks: encroached', '{:,}'.format(n), enc_max))
    rep.line('plot checks: upstream area', n, bad_upa)
    rep.line('plot checks: reasons, codes and wording', n, bad_reasons,
             '%d differ from the app through encroached alone' % len(by_encroached))
    for e in by_encroached[:3]:
        print('      e.g. %s' % (e,))
    rep.line('plot checks: route, step by step', n, bad_route)
    rep.line('R8 derived facts against siteContext()', len(r8), bad_r8)
    rep.line('R9 route totals against traceDownstream()', sum(1 for v in r9.values() if v), bad_r9,
             '%d off by the 0.1 m rounding of each step (at most %.1f m); %d first step(s) judged by '
             'the plot, not its cell' % (r9_rounding, r9_gap, r9_first))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--plots', type=int, default=5000, help='plots to read and trace (default 5000)')
    ap.add_argument('--grid', type=int, default=10000, help='measurement records on the thresholds (default 10000)')
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--node', default=shutil.which('node') or 'node')
    ap.add_argument('--db', help='also check the plot checks and certificates stored in this database')
    ap.add_argument('--sample', type=int, default=2000, help='plot checks to sample with --db (default 2000)')
    args = ap.parse_args()
    if args.plots < 1 or args.grid < 1 or args.sample < 0:
        ap.error('--plots and --grid must be at least 1, --sample at least 0')

    rng = random.Random(args.seed)
    layers = Layers(CITY)
    pl = plots(rng, args.plots)
    py_sites = [layers.read_site(lon, lat, plot_m) for lon, lat, plot_m, _ in pl]
    site_records = [{k: s[k] for k in SITE_FIELDS + ('encroached',)} for s in py_sites]
    grid = decide_grid(rng, args.grid)
    inputs = dict(city=CITY, upaCodes=list(range(256)), numbers=random_numbers(rng),
                  strings=random_strings(rng), plots=pl,
                  decide=[[m, rule] for m, (_, _, _, rule) in zip(site_records, pl)] + grid)

    n_strings = len(inputs['strings'])
    if args.db:
        certs, rows, r8, r9 = load_stored(args.db, args.sample, rng)
        inputs['strings'] += [c['canon'] for c in certs]
        inputs['contexts'] = [[float(r['lon']), float(r['lat']), r['plotM'], r['rule'], r['storage']] for r in rows]

    print('Python ports (dbms/bluetrace_logic.py) against the app (blue-grid/index.html) in headless Chromium;')
    print('examples of a difference are shown as (input, app, Python)')
    tmp = tempfile.mkdtemp(prefix='check_ports_')
    atexit.register(shutil.rmtree, tmp, True)
    app = run_app(inputs, args.node, tmp)
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
    bad = [(s[:24], a, fnv1a(s)) for s, a in zip(inputs['strings'][:n_strings], app['fnv1a']) if a != fnv1a(s)]
    rep.line('fnv1a()', n_strings, bad)

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
    if args.db:
        check_stored(args.db, rep, layers, certs, rows, r8, r9, app['fnv1a'][n_strings:], app['contexts'])
    print('All checks agree.' if not rep.failed else 'Some checks differ (above).')
    sys.exit(1 if rep.failed else 0)


if __name__ == '__main__':
    main()
