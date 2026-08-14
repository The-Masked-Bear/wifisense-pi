/* History and sleep report.
 *
 * Reads the archive over REST rather than the live WebSocket: nothing on this
 * page changes at UI rates, and a socket would stream a waterfall nobody is
 * drawing.  Refreshes on demand and on a slow timer.
 *
 * The charts here are deliberately not Viz.Trace or Viz.Sparkline.  Both plot
 * against an array index, which is correct for a live window arriving at a
 * fixed rate and wrong for archived data: rows are missing wherever the service
 * was stopped, so index position and time stop agreeing and every gap silently
 * shifts everything after it.  These plot against a real time axis, so a gap
 * reads as a gap.
 */

'use strict';

const $ = (id) => document.getElementById(id);

const COL = {
  motion: '#4dd4c4',
  vital: '#c88bff',
  breath: '#7fd1ff',
  alert: '#ff9f4d',
  bad: '#ff5f6b',
  ok: '#4ade8a',
  grid: 'rgba(30,40,57,0.9)',
  dim: '#4b586e',
  muted: '#7c8aa3',
};

/* Thresholds are fetched so the reference lines match what the detector is
   really deciding on; these are only the fallback if that fetch fails. */
let TH = { motion_on_db: 6.0, presence_on_db: 4.5 };
let QUIET_DB = 5.0;

/* --------------------------------------------------------------- time helpers */

const pad2 = (n) => String(n).padStart(2, '0');

function hhmm(ts) {
  const d = new Date(ts * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function dayHour(ts) {
  const d = new Date(ts * 1000);
  return `${pad2(d.getDate())}/${pad2(d.getMonth() + 1)} ${pad2(d.getHours())}:00`;
}

function mins(m) {
  if (m === null || m === undefined) return '–';
  const h = Math.floor(m / 60);
  return h ? `${h}h ${pad2(Math.round(m % 60))}m` : `${Math.round(m)}m`;
}

/* ---------------------------------------------------------------- base canvas */

function fit(canvas) {
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(1, Math.floor(rect.width * dpr));
  const h = Math.max(1, Math.floor(rect.height * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  return { ctx: canvas.getContext('2d'), w, h, dpr };
}

/* Vertical hour/day gridlines. Chosen from the span so a 6 h view is ruled
   hourly and a 7 day view daily, instead of 168 unreadable lines. */
function gridStep(span) {
  const H = 3600;
  for (const s of [H, 2 * H, 3 * H, 6 * H, 12 * H, 24 * H, 48 * H, 7 * 24 * H]) {
    if (span / s <= 10) return s;
  }
  return 7 * 24 * H;
}

function drawGrid(ctx, w, h, dpr, start, end) {
  const span = Math.max(1, end - start);
  const step = gridStep(span);
  ctx.strokeStyle = COL.grid;
  ctx.lineWidth = dpr;
  // Align to local midnight so day boundaries land on the line, not near it.
  const first = new Date(start * 1000);
  first.setMinutes(0, 0, 0);
  let t = Math.ceil(first.getTime() / 1000 / step) * step;
  for (; t < end; t += step) {
    const x = Math.round(((t - start) / span) * w) + 0.5;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
  }
}

/* ------------------------------------------------------------------ dB chart */

/* Plots mean as a line with peak as a translucent band above it.  Both are
   stored precisely so this pairing is visible: the band is where movement
   actually happened, the line is how sustained it was. */
function drawBands(canvas, rows, start, end, series, opts = {}) {
  const { ctx, w, h, dpr } = fit(canvas);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#080b12';
  ctx.fillRect(0, 0, w, h);
  drawGrid(ctx, w, h, dpr, start, end);

  const lo = opts.min !== undefined ? opts.min : 0;
  const hi = opts.max !== undefined ? opts.max : 35;
  const span = Math.max(1, end - start);
  const pad = 3 * dpr;
  const xOf = (t) => ((t - start) / span) * w;
  const yOf = (v) => h - pad - ((Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo)) * (h - pad * 2);

  for (const th of opts.thresholds || []) {
    const y = Math.round(yOf(th.value)) + 0.5;
    ctx.save();
    ctx.setLineDash([4 * dpr, 4 * dpr]);
    ctx.strokeStyle = th.color;
    ctx.lineWidth = dpr;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
    ctx.restore();
  }

  if (!rows.length) {
    ctx.fillStyle = COL.dim;
    ctx.font = `${11 * dpr}px ui-monospace, monospace`;
    ctx.fillText('no data in this range', 8 * dpr, h / 2);
    return;
  }

  for (const s of series) {
    // Peak band first so the mean line sits on top of it.
    if (s.peak) {
      ctx.fillStyle = s.band;
      ctx.beginPath();
      let open = false;
      for (const r of rows) {
        const v = r[s.peak];
        if (v === null || v === undefined) continue;
        const x = xOf(r.t);
        if (!open) { ctx.moveTo(x, yOf(lo)); open = true; }
        ctx.lineTo(x, yOf(v));
      }
      if (open) {
        ctx.lineTo(xOf(rows[rows.length - 1].t), yOf(lo));
        ctx.closePath();
        ctx.fill();
      }
    }
    // Mean line, broken across nulls so a gap is not bridged.
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.4 * dpr;
    ctx.lineJoin = 'round';
    let drawing = false;
    ctx.beginPath();
    for (const r of rows) {
      const v = r[s.key];
      if (v === null || v === undefined) { drawing = false; continue; }
      const x = xOf(r.t), y = yOf(v);
      if (drawing) ctx.lineTo(x, y);
      else { ctx.moveTo(x, y); drawing = true; }
    }
    ctx.stroke();
  }

  // y scale hint, top-left, so the axis is readable without a separate gutter.
  ctx.fillStyle = COL.dim;
  ctx.font = `${9.5 * dpr}px ui-monospace, monospace`;
  ctx.fillText(opts.unit || '', 6 * dpr, 12 * dpr);
}

/* -------------------------------------------------------------- occupancy band */

/* One pixel column per bucket, coloured by what the room was doing.  A band
   rather than a line: occupancy is categorical, and drawing it as a 0/1 line
   invites reading the transitions as a magnitude. */
function drawOccupancy(canvas, rows, start, end) {
  const { ctx, w, h, dpr } = fit(canvas);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#080b12';
  ctx.fillRect(0, 0, w, h);
  if (!rows.length) return;

  const span = Math.max(1, end - start);
  const xOf = (t) => ((t - start) / span) * w;
  // Width from the median gap, so a decimated series still tiles without
  // leaving hairline gaps that look like missing data.
  const step = rows.length > 1 ? (rows[1].t - rows[0].t) : 60;
  const cw = Math.max(1, (step / span) * w + 1);

  for (const r of rows) {
    const occ = r.occupied || 0;
    if (occ < 0.5) continue;
    const peak = r.motion_max_db;
    const restless = peak !== null && peak !== undefined && peak >= QUIET_DB;
    ctx.fillStyle = restless ? 'rgba(255,159,77,0.85)' : 'rgba(77,212,196,0.75)';
    ctx.fillRect(xOf(r.t), 0, cw, h);
  }
  drawGrid(ctx, w, h, dpr, start, end);
}

/* ------------------------------------------------------------------- axis row */

function setAxis(el, start, end) {
  if (!el) return;
  const mid = start + (end - start) / 2;
  const long = (end - start) > 36 * 3600;
  const f = long ? dayHour : hhmm;
  el.innerHTML = `<span>${f(start)}</span><span>${f(mid)}</span><span>${f(end)}</span>`;
}

/* --------------------------------------------------------------------- ranges */

const RANGES = [
  { label: '6h', hours: 6 },
  { label: '12h', hours: 12 },
  { label: '24h', hours: 24 },
  { label: '3d', hours: 72 },
  { label: '7d', hours: 168 },
];
let activeRange = 12;
let lastHistory = null;

function buildRangeButtons() {
  const box = $('rangeBtns');
  box.innerHTML = '';
  for (const r of RANGES) {
    const b = document.createElement('button');
    b.className = 'ghost' + (r.hours === activeRange ? ' on' : '');
    b.textContent = r.label;
    b.addEventListener('click', () => {
      activeRange = r.hours;
      buildRangeButtons();
      loadHistory();
    });
    box.appendChild(b);
  }
}

/* ---------------------------------------------------------------------- loads */

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

function renderHistory(d) {
  lastHistory = d;
  const { start, end } = d;
  const sense = d.sense || [];
  const env = d.env || [];

  drawBands($('dbChart'), sense, start, end, [
    { key: 'motion_db', peak: 'motion_max_db', color: COL.motion, band: 'rgba(77,212,196,0.16)' },
    { key: 'vital_db', color: COL.vital, band: 'rgba(200,139,255,0.14)' },
  ], {
    min: 0, max: 35, unit: 'dB',
    thresholds: [
      { value: TH.motion_on_db, color: 'rgba(77,212,196,0.45)' },
      { value: TH.presence_on_db, color: 'rgba(200,139,255,0.40)' },
    ],
  });

  drawOccupancy($('occChart'), sense, start, end);

  drawBands($('bpmChart'), sense, start, end,
    [{ key: 'bpm', color: COL.breath, band: 'rgba(127,209,255,0.14)' }],
    { min: 6, max: 38, unit: 'bpm' });

  drawBands($('envChart'), env, start, end, [
    { key: 'temp_c', color: COL.alert, band: 'rgba(255,159,77,0.12)' },
    { key: 'humidity', color: COL.breath, band: 'rgba(127,209,255,0.10)' },
    { key: 'aqi', color: COL.ok, band: 'rgba(74,222,138,0.10)' },
  ], { min: 0, max: 150, unit: '°C · %RH · AQI' });

  setAxis($('histAxis'), start, end);
  const rows = sense.length;
  $('rangeInfo').textContent = rows
    ? `${rows} points · ${d.step}s each`
    : 'no data in this range';
}

async function loadHistory() {
  try {
    renderHistory(await getJSON(`/api/history?hours=${activeRange}&points=900`));
  } catch (err) {
    $('rangeInfo').textContent = String(err.message || err);
  }
}

/* ---------------------------------------------------------------------- sleep */

function renderSleep(r) {
  const body = $('sleepBody');
  const found = !!r.found;
  body.style.opacity = found ? '1' : '0.5';

  $('sleepRange').textContent = found
    ? `${hhmm(r.in_bed_start)} → ${hhmm(r.in_bed_end)}`
    : (r.reason || '–');

  if (!found) {
    for (const id of ['sIn', 'sStill', 'sRest', 'sBpm']) $(id).textContent = '–';
    for (const id of ['sInSub', 'sStillSub', 'sRestSub', 'sBpmSub']) $(id).textContent = '–';
    $('sWakes').innerHTML = `<div class="cell"><span>result</span><b>${r.reason || 'no data'}</b></div>`;
    drawBands($('nightChart'), [], 0, 1, [], { min: 0, max: 35 });
    setAxis($('nightAxis'), r.window ? r.window[0] : 0, r.window ? r.window[1] : 1);
    return;
  }

  $('sIn').textContent = mins(r.in_bed_minutes);
  $('sInSub').textContent = `${hhmm(r.in_bed_start)} → ${hhmm(r.in_bed_end)}`;

  $('sStill').textContent = mins(r.still_minutes);
  const pct = r.stillness !== null && r.stillness !== undefined
    ? `${Math.round(r.stillness * 100)}% of time in bed` : '–';
  $('sStillSub').textContent = pct;
  $('sStill').className = (r.stillness || 0) >= 0.85 ? 'good'
    : ((r.stillness || 0) >= 0.7 ? 'warn' : 'bad');

  $('sRest').textContent = mins(r.restless_minutes);
  $('sRestSub').textContent = `${r.awakening_count} disturbance${r.awakening_count === 1 ? '' : 's'}`;
  $('sRest').className = r.awakening_count === 0 ? 'good' : (r.awakening_count <= 3 ? 'warn' : 'bad');

  const b = r.breathing || {};
  $('sBpm').textContent = b.mean_bpm !== null && b.mean_bpm !== undefined ? b.mean_bpm.toFixed(1) : '–';
  $('sBpmSub').textContent = (b.min_bpm !== null && b.min_bpm !== undefined)
    ? `${b.min_bpm}–${b.max_bpm} bpm · ${Math.round((b.coverage || 0) * 100)}% covered`
    : 'no lock held';

  $('sWakes').innerHTML = (r.awakenings && r.awakenings.length)
    ? r.awakenings.map((a) =>
        `<div class="cell"><span>${hhmm(a.at)}</span><b class="warn">${mins(a.seconds / 60)}</b></div>`).join('')
    : '<div class="cell"><span>none</span><b class="good">undisturbed</b></div>';

  const tl = r.timeline || [];
  drawBands($('nightChart'), tl, r.in_bed_start, r.in_bed_end, [
    { key: 'motion_db', peak: 'motion_max_db', color: COL.motion, band: 'rgba(77,212,196,0.16)' },
    { key: 'vital_db', color: COL.vital, band: 'rgba(200,139,255,0.14)' },
    { key: 'bpm', color: COL.breath, band: 'rgba(127,209,255,0.12)' },
  ], {
    min: 0, max: 40, unit: 'dB · bpm',
    thresholds: [{ value: QUIET_DB, color: 'rgba(255,159,77,0.5)' }],
  });
  setAxis($('nightAxis'), r.in_bed_start, r.in_bed_end);
}

async function loadSleep(night) {
  try {
    renderSleep(await getJSON('/api/sleep' + (night ? `?night=${night}` : '')));
  } catch (err) {
    $('sleepRange').textContent = String(err.message || err);
  }
}

async function loadNights() {
  const sel = $('nightPick');
  try {
    const d = await getJSON('/api/sleep/nights?limit=30');
    const nights = d.nights || [];
    if (!nights.length) {
      sel.innerHTML = '<option value="">no nights recorded yet</option>';
      return null;
    }
    sel.innerHTML = nights.map((n) => `<option value="${n}">${n}</option>`).join('');
    return nights[0];
  } catch {
    sel.innerHTML = '<option value="">unavailable</option>';
    return null;
  }
}

/* ----------------------------------------------------------------------- init */

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (lastHistory) renderHistory(lastHistory);
    loadSleep($('nightPick').value || undefined);
  }, 150);
});

$('nightPick').addEventListener('change', (e) => loadSleep(e.target.value || undefined));

(async function init() {
  // Thresholds first, so the very first paint already has the right reference
  // lines rather than drawing the defaults and then jumping.
  try {
    const cfg = await getJSON('/api/config');
    TH.motion_on_db = cfg.motion_on_db ?? TH.motion_on_db;
    TH.presence_on_db = cfg.presence_on_db ?? TH.presence_on_db;
  } catch { /* defaults stand */ }

  try {
    const st = await getJSON('/api/history/stats');
    if (!st.enabled) {
      $('offPanel').hidden = false;
      $('offWhy').textContent =
        'The long-term archive is disabled. Set "archive_enabled": true in pi/config.json and restart.';
      return;
    }
    const span = st.span || {};
    $('spanDays').textContent = span.days !== undefined ? span.days : '–';
    $('spanRows').textContent = span.rows !== undefined ? span.rows.toLocaleString('en-US') : '–';
    if (st.errors) {
      $('offPanel').hidden = false;
      $('offWhy').textContent = `Archive reported ${st.errors} write error(s): ${st.error}`;
    }
  } catch (err) {
    $('offPanel').hidden = false;
    $('offWhy').textContent = String(err.message || err);
    return;
  }

  $('quietDb').textContent = QUIET_DB;
  buildRangeButtons();
  await loadHistory();
  const latest = await loadNights();
  await loadSleep(latest || undefined);

  // Slow refresh: a 10 s bucket cannot change faster than that, and this page
  // is read rather than watched.
  setInterval(loadHistory, 30000);
})();
