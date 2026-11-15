// Runs functions of the BlueTrace app (blue-grid/index.html) in headless
// Chromium, on the inputs dbms/check_ports.py prepared, and writes what the app
// returns. check_ports.py compares that with the Python ports.
//
//     node dbms/check_ports_browser.js <app dir> <input.json> <output.json>
//
// Needs Playwright with Chromium (npm install playwright; npx playwright install
// chromium). The app is served from a local HTTP server; every other request is
// refused, so the check runs offline. The map library comes from a CDN and is
// not needed to compute anything, so a stand-in that accepts any call takes its
// place and lets the app's start-up code run. Everything measured below is the
// app's own code, unmodified.
'use strict';
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

const [appDir, inFile, outFile] = process.argv.slice(2);
if (!outFile) {
  console.error('usage: node dbms/check_ports_browser.js <app dir> <input.json> <output.json>');
  process.exit(2);
}

const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.json': 'application/json',
  '.geojson': 'application/geo+json', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.css': 'text/css', '.svg': 'image/svg+xml', '.txt': 'text/plain',
};

const MAP_STUB = `(() => {
  const handler = {
    get(target, key) {
      if (key === 'then' || key === Symbol.iterator) return undefined;  // not a promise, not iterable
      if (key === Symbol.toPrimitive) return () => 0;
      return stub();
    },
    apply() { return stub(); },
    construct() { return stub(); },
    set() { return true; },
  };
  function stub() { return new Proxy(function () {}, handler); }
  window.maplibregl = stub();
})();`;

// Executed inside the page, where the app's top-level functions and variables
// are globals. It loads the layers exactly as loadCity() does and the
// parameters exactly as the app does once corpus.json arrives.
async function runInApp(input) {
  const corpus = await (await fetch('data/corpus.json')).json();
  reconcileParams(corpus);
  const city = input.city, b = CITIES[city].bounds;
  state.city = city;
  state.samplers = {};
  for (const id of DATA_LAYERS) {
    state.samplers[id] = await makeSampler('data/' + city + '/' + id + '.png', b, true);
  }
  state.samplers.encroached = await makeSampler('data/' + city + '/encroached.png', b, false);
  state.centroids = null;   // only used to count structures along a route

  const S = state.samplers;
  const out = {
    params: Object.assign({}, PARAMS),
    uncertaintyM: UNCERTAINTY_M,
    layers: Object.keys(S).filter((k) => S[k]),
    decodeUpa: input.upaCodes.map(decodeUpa),
    sig2: input.numbers.map(sig2),
    fnv1a: input.strings.map(fnv1a),
  };
  out.sites = input.plots.map(([lon, lat, plotM]) => {
    state.plotM = plotM;
    return readSite({ lng: lon, lat: lat });
  });
  // the verdict the app reaches from its own reading of each plot
  out.siteVerdicts = input.plots.map(([lon, lat, plotM, ruleId], i) => {
    const r = decide(out.sites[i], RULESETS[ruleId]);
    return [r.d, r.codes];
  });
  // each step of a route: the move, its length, and the upstream area and
  // height above drainage the app reads at the cell it reaches (route_step's columns)
  function stepsOf(path) {
    const steps = [];
    for (let i = 1; i < path.length; i++) {
      const [lon0, lat0] = path[i - 1], [lon1, lat1] = path[i];
      const upaCode = sampleCh(S.hydro, lon1, lat1, b, 0, 'max', 0, 1);
      const hand = sampleCh(S.history, lon1, lat1, b, 2, 'min', 0, 1);
      steps.push([lon1 - lon0, lat1 - lat0, metresBetween(lon0, lat0, lon1, lat1),
                  upaCode === null ? null : sig2(decodeUpa(upaCode)),
                  hand !== null && hand < 255 ? hand : null]);
    }
    return steps;
  }
  out.traces = input.plots.map(([lon, lat]) => {
    const t = traceDownstream({ lng: lon, lat: lat });
    return { endedBy: t.endedBy, steps: stepsOf(t.path) };
  });
  // siteContext(): what the app shows for a plot check, end to end
  out.contexts = (input.contexts || []).map(([lon, lat, plotM, ruleId, storage]) => {
    state.plotM = plotM;
    state.storageLpm2 = storage;
    const c = siteContext({ lng: lon, lat: lat }, RULESETS[ruleId]), r = c.rec;
    const rec = {};
    ['plotAreaM2', 'retreatM', 'flowClass', 'handClass', 'upaKm2', 'runoffDisplacedM3', 'runoffLoM3',
     'runoffHiM3', 'runoffAnnualM3', 'tankerEquiv', 'retentionRequiredM3', 'rwhRequiredM3',
     'retentionShortfallM3'].forEach((k) => { rec[k] = r[k]; });
    // the height above drainage traceDownstream() reads where the route starts:
    // the centre of the 90 m cell the plot falls in, not the plot itself
    const h0 = sampleCh(S.history, Math.round(lon / MERIT_DEG) * MERIT_DEG,
                        Math.round(lat / MERIT_DEG) * MERIT_DEG, b, 2, 'min', 0, 1);
    return { m: c.m, verdict: [c.verdict.d, c.verdict.codes, c.verdict.why], rec: rec,
             route: { endedBy: c.route.endedBy, lengthM: c.route.lengthM, lowGroundM: c.route.lowGroundM,
                      originHand: h0 !== null && h0 < 255 ? h0 : null, steps: stepsOf(c.route.path) } };
  });
  out.decisions = input.decide.map(([m, ruleId]) => {
    const r = decide(m, RULESETS[ruleId]);
    return [r.d, r.codes, r.why];
  });
  return out;
}

(async () => {
  const input = JSON.parse(fs.readFileSync(inFile, 'utf8'));
  const root = path.resolve(appDir);
  const server = http.createServer((req, res) => {
    const rel = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    let file = path.join(root, rel);
    if (file !== root && !file.startsWith(root + path.sep)) { res.writeHead(403); res.end(); return; }
    if (rel.endsWith('/')) file = path.join(file, 'index.html');
    fs.readFile(file, (err, data) => {
      if (err) { res.writeHead(404); res.end(); return; }
      res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
      res.end(data);
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;

  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    await page.route('**/*', (route) =>
      route.request().url().startsWith(base) ? route.continue() : route.abort());
    await page.addInitScript(MAP_STUB);
    await page.goto(base + '/index.html', { waitUntil: 'load' });
    await page.waitForFunction(() => typeof decide === 'function' && typeof state === 'object'
                                      && typeof RULESETS === 'object');
    const out = await page.evaluate(runInApp, input);
    out.pageErrors = pageErrors;
    fs.writeFileSync(outFile, JSON.stringify(out));
  } finally {
    await browser.close();
    server.close();
  }
})().catch((err) => { console.error(err); process.exit(1); });
