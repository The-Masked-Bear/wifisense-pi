/* Dashboard client: one WebSocket, one render path. */

'use strict';

const $ = (id) => document.getElementById(id);

const els = {
  linkDot: $('linkDot'), linkName: $('linkName'),
  statRate: $('statRate'), statRssi: $('statRssi'), statNode: $('statNode'),
  stateLabel: $('stateLabel'), stateSub: $('stateSub'),
  motionDb: $('motionDb'), vitalDb: $('vitalDb'),
  motionBar: $('motionBar'), vitalBar: $('vitalBar'),
  bpmValue: $('bpmValue'), bpmConf: $('bpmConf'), breathReason: $('breathReason'),
  diag: $('diag'),
  btnRecord: $('btnRecord'), btnReset: $('btnReset'), rampSelect: $('rampSelect'),
  surface: $('surface'), waterfallCanvas: $('waterfall'),
  specPanel: $('specPanel'), specFall: $('specFall'), specEvents: $('specEvents'),
  specRange: $('specRange'), specAxis: $('specAxis'),
  specPeak: $('specPeak'), specPeakF: $('specPeakF'),
  viewToggle: $('viewToggle'), axisNote: $('axisNote'), viewLegend: $('viewLegend'),
  envPanel: $('envPanel'), envSensors: $('envSensors'), envAge: $('envAge'),
  envTemp: $('envTemp'), envTempSrc: $('envTempSrc'), envTempFoot: $('envTempFoot'),
  envHum: $('envHum'), envHumFoot: $('envHumFoot'),
  envPress: $('envPress'), envPressFoot: $('envPressFoot'),
  envAir: $('envAir'), envAirFoot: $('envAirFoot'), envAirCard: $('envAirCard'),
  pktPanel: $('pktPanel'), pktSource: $('pktSource'), pktWire: $('pktWire'),
  pktTotal: $('pktTotal'), pktTotalSub: $('pktTotalSub'),
  pktDelivery: $('pktDelivery'), pktDeliverySub: $('pktDeliverySub'),
  pktLost: $('pktLost'), pktLostSub: $('pktLostSub'),
  pktBad: $('pktBad'), pktBadSub: $('pktBadSub'),
  pktTypes: $('pktTypes'), pktIntegrity: $('pktIntegrity'),
  pktRadio: $('pktRadio'), pktRadioGroup: $('pktRadioGroup'),
  pktEndpoints: $('pktEndpoints'),
};

const waterfall = new Viz.Waterfall($('waterfall'), { bins: 32, ramp: localStorage.getItem('ramp') || 'viridis' });
const trace = new Viz.Trace($('trace'), {
  min: -5, max: 30,
  thresholds: [
    { value: 6.0, color: 'rgba(77,212,196,0.22)' },
    { value: 4.5, color: 'rgba(200,139,255,0.20)' },
  ],
});
/* Sub-GHz waterfall. Inferno rather than viridis so it reads as a different
   instrument at a glance and does not get confused with the CSI panel. */
const specFall = els.specFall
  ? new Viz.Waterfall(els.specFall, { bins: 32, ramp: 'inferno', columnWidth: 6 })
  : null;
let specAxisDone = false;

const waveform = new Viz.Waveform($('waveform'));
const spectrum = new Viz.Spectrum($('spectrum'));

/* Environment sparklines. Each is coloured to match its card so the four read
   as separate instruments rather than one four-line chart. */
const sparks = {
  tc:  $('sparkTemp')  ? new Viz.Sparkline($('sparkTemp'),  { color: '#ff9f4d', fill: 'rgba(255,159,77,0.13)' }) : null,
  rh:  $('sparkHum')   ? new Viz.Sparkline($('sparkHum'),   { color: '#7fd1ff', fill: 'rgba(127,209,255,0.13)' }) : null,
  hpa: $('sparkPress') ? new Viz.Sparkline($('sparkPress'), { color: '#c88bff', fill: 'rgba(200,139,255,0.13)' }) : null,
  ppm: $('sparkAir')   ? new Viz.Sparkline($('sparkAir'),   { color: '#4ade8a', fill: 'rgba(74,222,138,0.13)' }) : null,
};
let envSized = false;

/* The 3D surface shows the same measurement as the 2D waterfall, so both are
   fed every row and only visibility switches. Keeping them in sync means
   toggling views never shows a gap in history. */
/* The 3D view is optional in every direction: the markup may not carry it, the
   script may not have loaded, or the GPU may not support it.  None of those may
   take the dashboard down with them -- an earlier revision let one missing
   element throw during init, which aborted the rest of this file and left the
   page permanently on "connecting...". */
const has3dDom = !!(els.surface && els.viewToggle && els.axisNote && els.viewLegend);
const surface = (has3dDom && typeof Surface3D === 'function')
  ? new Surface3D(els.surface, { bins: 32, depth: 1500, ramp: localStorage.getItem('ramp') || 'viridis' })
  : { ok: false, resize() {}, setVisible() {}, push() {}, pushMany() {}, render() {}, clear() {}, setRamp() {} };
// ?view=3d selects a view without touching the stored preference, which makes
// the choice linkable and lets kiosk mode boot straight into the 3D display.
const urlView = new URLSearchParams(location.search).get('view');
let view = surface.ok
  ? (urlView === '3d' || urlView === '2d' ? urlView : (localStorage.getItem('view') || '2d'))
  : '2d';

function applyView() {
  if (!has3dDom) return;
  const is3d = view === '3d';
  els.waterfallCanvas.hidden = is3d;
  surface.setVisible(is3d);
  els.axisNote.hidden = true;
  els.viewLegend.textContent = is3d
    ? 'frequency \u00d7 time \u00d7 amplitude'
    : 'sub-carrier \u2191 \u00b7 time \u2192';
  for (const b of els.viewToggle.querySelectorAll('.seg-btn')) {
    b.classList.toggle('on', b.dataset.view === view);
  }
  // Whichever canvas was hidden ignored every window resize while it was, so the
  // one being revealed re-fits itself here. setVisible() does that for the 3D
  // canvas; the 2D waterfall rescales its retained history into the new size.
  if (!is3d) waterfall.resize();
}

if (!surface.ok) {
  // No WebGL (or no vertex texture fetch): hide the control rather than offer
  // a button that silently does nothing.
  if (els.viewToggle) els.viewToggle.style.display = 'none';
  if (els.surface) els.surface.hidden = true;
  if (els.axisNote) els.axisNote.hidden = true;
} else {
  // If the GPU drops the context, fall back to the 2D waterfall rather than
  // leaving a blank panel.
  surface.onContextLost = () => {
    if (view === '3d') { view = '2d'; applyView(); }
    if (els.viewToggle) els.viewToggle.style.display = 'none';
  };
  surface.onContextRestored = () => {
    if (els.viewToggle) els.viewToggle.style.display = '';
  };

  els.viewToggle.addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    view = btn.dataset.view;
    localStorage.setItem('view', view);
    applyView();
  });
}
applyView();

/* One rAF loop drives the 3D view. The 2D canvases redraw on data arrival
   instead, since they have nothing to animate between updates. */
function frame() {
  if (view === '3d') surface.render();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

els.rampSelect.value = localStorage.getItem('ramp') || 'viridis';
els.rampSelect.addEventListener('change', () => {
  waterfall.setRamp(els.rampSelect.value);
  if (surface.ok) surface.setRamp(els.rampSelect.value);
  localStorage.setItem('ramp', els.rampSelect.value);
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    waterfall.resize(); trace.resize(); waveform.resize(); spectrum.resize();
    if (surface.ok) surface.resize();
    if (specFall) specFall.resize();
    if (latest) render(latest);
    // After render, and from the held copy: the latest snapshot is usually a
    // fast update carrying no env payload, so re-rendering from it alone would
    // leave the four sparklines stretched at their pre-resize bitmap size.
    if (held.env && els.envPanel && !els.envPanel.hidden) {
      for (const k in sparks) if (sparks[k]) sparks[k].resize();
      renderEnv(held.env);
    }
  }, 140);
});

/* ------------------------------------------------------------------ helpers */

const STATE_TEXT = {
  empty:    ['CLEAR',    'no one detected'],
  still:    ['PRESENT',  'stationary — vital signs detected'],
  subtle:   ['MOVEMENT', 'small movements'],
  active:   ['ACTIVE',   'walking or gesturing'],
  vigorous: ['VIGOROUS', 'rapid movement'],
};

function fmt(v, digits = 1) {
  return (v === null || v === undefined || Number.isNaN(v)) ? '–' : Number(v).toFixed(digits);
}

function cell(label, value, cls) {
  return `<div class="cell"><span>${label}</span><b class="${cls || ''}">${value}</b></div>`;
}

/* Counters here reach into the millions over a long run, and an unseparated
   seven-digit number is genuinely hard to compare against the one next to it. */
function num(v) {
  return (v === null || v === undefined || Number.isNaN(v)) ? '–' : Number(v).toLocaleString('en-US');
}

function bytes(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '–';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, n = Number(v);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

/* ------------------------------------------------------------------- render */

let latest = null;
let recording = false;

/* Slow-changing panels arrive only on "full" updates, so their last values are
   retained between them rather than being blanked out. */
const held = { link: {}, stimulus: {}, node: null, breathing: {}, series: null, env: null };

/* ------------------------------------------------------------- environment */

/* Plain-language advice per AQI band. The category name itself already comes
   from the server; this is the "so what do I do about it" half. */
const AIR_ADVICE = {
  good: 'well ventilated',
  moderate: 'acceptable',
  sensitive: 'worth opening a window',
  unhealthy: 'ventilate now',
  'very-unhealthy': 'ventilate now',
  hazardous: 'ventilate now',
};

/* Sparkline colours matching the AQI category swatches in app.css. Kept here
   rather than read back from CSS because the canvas needs the literal value
   and a computed-style round trip per update is not worth it for six colours. */
const AIR_COLOURS = {
  good:             ['#4ade8a', 'rgba(74,222,138,0.13)'],
  moderate:         ['#f2d54e', 'rgba(242,213,78,0.13)'],
  sensitive:        ['#ff9f4d', 'rgba(255,159,77,0.13)'],
  unhealthy:        ['#ff5f6b', 'rgba(255,95,107,0.13)'],
  'very-unhealthy': ['#c88bff', 'rgba(200,139,255,0.13)'],
  hazardous:        ['#ff3b3b', 'rgba(255,59,59,0.16)'],
  // Deliberately grey: "no trustworthy reading", not a place on the scale.
  stale:            ['#7c8aa3', 'rgba(124,138,163,0.13)'],
};

function setCard(card, present) {
  if (card) card.classList.toggle('absent', !present);
}

function renderEnv(e) {
  if (!els.envPanel) return;
  const wasHidden = els.envPanel.hidden;
  els.envPanel.hidden = false;
  // The cards are inside a hidden panel until the first environment frame, so
  // every canvas measured 0x0 at construction. Size them the moment they are
  // actually laid out, or the sparklines draw into a one-pixel bitmap.
  if (wasHidden || !envSized) {
    for (const k in sparks) if (sparks[k]) sparks[k].resize();
    envSized = true;
  }

  const sens = e.sensors || {};
  const series = e.series || [];
  const col = (key) => series.map(p => (p[key] === undefined ? null : p[key]));

  /* temperature ----------------------------------------------------------- */
  els.envTemp.textContent = e.temp_c === null || e.temp_c === undefined ? '––' : fmt(e.temp_c, 1);
  els.envTempSrc.textContent = e.temp_source || '';
  // Showing both when both are present is the honest thing to do: they will
  // disagree by around a degree, and a reader who sees only one number and
  // then spots the other elsewhere would reasonably conclude something is
  // broken.
  els.envTempFoot.textContent = (sens.bmp280 && sens.dht22)
    ? `bmp ${fmt(e.bmp_temp_c, 1)}°  ·  dht ${fmt(e.dht_temp_c, 1)}°`
    : (e.temp_source ? `single source` : 'no sensor');
  setCard(els.envTemp.closest('.env-card'), sens.bmp280 || sens.dht22);
  if (sparks.tc) sparks.tc.draw(col('tc'));

  /* humidity -------------------------------------------------------------- */
  els.envHum.textContent = sens.dht22 ? fmt(e.humidity, 1) : '––';
  els.envHumFoot.textContent = sens.dht22
    ? (e.dew_point_c !== null && e.dew_point_c !== undefined
        ? `dew point ${fmt(e.dew_point_c, 1)}°C` : '')
    : (e.dht_fail ? `no reply (${e.dht_fail} tries)` : 'no sensor');
  setCard(els.envHum.closest('.env-card'), !!sens.dht22);
  if (sparks.rh) sparks.rh.draw(col('rh'));

  /* pressure -------------------------------------------------------------- */
  els.envPress.textContent = sens.bmp280 ? fmt(e.pressure_hpa, 1) : '––';
  els.envPressFoot.textContent = sens.bmp280
    ? (e.altitude_m !== null && e.altitude_m !== undefined
        ? `≈ ${fmt(e.altitude_m, 0)} m altitude` : '')
    : 'no sensor';
  setCard(els.envPress.closest('.env-card'), !!sens.bmp280);
  if (sparks.hpa) sparks.hpa.draw(col('hpa'));

  /* air quality ----------------------------------------------------------- */
  const calibrated = !!sens.mq135_calibrated;
  els.envAir.textContent = (calibrated && e.aqi !== null && e.aqi !== undefined)
    ? String(e.aqi) : '––';
  // A stale baseline is deliberately NOT painted green. The index is pinned at
  // the bottom of the scale for a reason that has nothing to do with the air,
  // and colouring that "Good" would be the one genuinely misleading state this
  // panel can reach.
  els.envAir.className = e.baseline_stale ? 'aqi-stale'
    : (e.aqi_category ? `aqi-${e.aqi_category}` : '');
  if (!sens.mq135) {
    els.envAirFoot.textContent = e.gas_mv ? `out of range (${e.gas_mv} mV)` : 'no sensor';
  } else if (!calibrated) {
    els.envAirFoot.textContent = `warming up · ${e.gas_mv} mV`;
  } else if (e.baseline_stale) {
    els.envAirFoot.textContent =
      `baseline drifted — self-correcting\n${fmt(e.gas_ppm, 0)} ppm is below outdoor air`;
  } else {
    // Two deliberate lines: the EPA category name is up to 29 characters and
    // would push everything else off the end of a single one. Keeping the raw
    // ppm visible underneath is what stops the AQI number from reading as a
    // measurement it is not.
    // The correction count earns its place: it is the only outward sign that
    // the baseline is being maintained rather than quietly rotting.
    const abc = e.gas_abc ? ` · abc ${e.gas_abc}` : '';
    const line2 = [AIR_ADVICE[e.aqi_category], `${fmt(e.gas_ppm, 0)} ppm CO₂e${abc}`]
      .filter(Boolean).join(' · ');
    els.envAirFoot.textContent = `${e.aqi_label || ''}\n${line2}`;
  }
  setCard(els.envAirCard, !!sens.mq135);
  if (sparks.ppm) {
    const c = e.baseline_stale ? AIR_COLOURS.stale
      : (AIR_COLOURS[e.aqi_category] || AIR_COLOURS.good);
    sparks.ppm.setColor(c[0], c[1]);
    // Plot ppm rather than the index while the baseline is stale: the index is
    // pinned at 0 and would draw a flat line saying nothing, whereas the raw
    // ppm still shows the drift that is the actual story.
    sparks.ppm.draw(col(e.baseline_stale ? 'ppm' : 'aqi'));
  }

  /* header ---------------------------------------------------------------- */
  const mark = (name, ok) => `${name} ${ok ? '✓' : '✕'}`;
  els.envSensors.textContent = [
    mark('bmp280', sens.bmp280), mark('dht22', sens.dht22), mark('mq135', sens.mq135),
  ].join('  ·  ');
  const age = e.age;
  els.envAge.textContent = (age === null || age === undefined) ? '–' : `${fmt(age, 0)} s ago`;
  els.envAge.style.color = age > 15 ? 'var(--bad)' : '';
}

/* ----------------------------------------------------------------- packets */

/* Every figure here is already in the snapshot; this panel only groups and
   labels them. Nothing is recomputed server-side for it, which is why it costs
   no extra bytes on the wire.

   The one thing worth being careful about is what is NOT summed. Fragment loss
   and sequence-gap loss are the same lost frames counted at two different
   layers -- a frame that loses a radio fragment never reaches the framer, so
   its sequence number is missing too. Adding them would double-count the
   entire loss figure, so they stay in separate groups. */
function renderPackets(stats, link, node, stim) {
  if (!els.pktPanel) return;

  const dec = link.decode || {};
  const radio = link.radio || null;
  const reasm = (radio && radio.reassembly) || null;
  const crypto = (radio && radio.crypto && typeof radio.crypto === 'object') ? radio.crypto : null;

  const arrived = stats.frames_in || 0;
  const lost = stats.dropped_by_seq || 0;
  const expected = arrived + lost;
  const corrupt = (dec.crc_errors || 0) + (dec.cobs_errors || 0)
                + (dec.short_frames || 0) + (dec.unknown_types || 0);

  /* headline ------------------------------------------------------------- */
  els.pktTotal.textContent = num(dec.frames_ok);
  els.pktTotalSub.textContent = `${num(arrived)} CSI · ${fmt(stats.throughput_hz, 1)} Hz`;

  // Thresholds are calibrated per transport, because "good" is not the same
  // number on each.  A cable is lossless and anything under ~99% on one means
  // a bad lead; the nRF24 sits at ~88% by design -- a frame is five packets and
  // survives only if all five do, against a 3-deep hardware RX FIFO.  Painting
  // that documented, perfectly serviceable state red would train the reader to
  // ignore the colour, so the radio's band starts where its measured normal is.
  // 88% of 100 Hz is still 4x what respiration needs.
  const wireless = !!reasm;
  const okAt = wireless ? 80 : 99;
  const warnAt = wireless ? 60 : 90;
  const grade = (pct) => (pct >= okAt ? 'good' : (pct >= warnAt ? 'warn' : 'bad'));

  // Undefined rather than 100% until something has actually arrived: a link
  // that has delivered nothing is not a perfect link.
  if (expected > 0) {
    const pct = (arrived / expected) * 100;
    els.pktDelivery.textContent = `${pct.toFixed(pct >= 99.95 ? 1 : 2)}%`;
    els.pktDelivery.className = grade(pct);
    els.pktDeliverySub.textContent = `${num(arrived)} of ${num(expected)} sent`;
  } else {
    els.pktDelivery.textContent = '–';
    els.pktDelivery.className = '';
    els.pktDeliverySub.textContent = 'no frames yet';
  }

  // Same scale as delivery, so the two cards can never disagree about whether
  // the link is healthy.
  els.pktLost.textContent = num(lost);
  els.pktLost.className = lost === 0 ? 'good'
    : (expected ? grade(100 - (lost / expected) * 100) : 'warn');
  els.pktLostSub.textContent = expected
    ? `${((lost / expected) * 100).toFixed(2)}% of the stream` : 'sequence gaps';

  els.pktBad.textContent = num(corrupt);
  els.pktBad.className = corrupt === 0 ? 'good' : 'warn';
  els.pktBadSub.textContent = corrupt === 0
    ? 'every frame intact' : 'rejected by CRC / framing';

  /* header legends -------------------------------------------------------- */
  els.pktSource.textContent = link.name
    ? `${link.name}${node && node.link_mode ? ` · node ${node.link_mode}` : ''}`
    : '–';
  const secs = stats.uptime || 0;
  const kbits = secs > 0 ? ((dec.bytes_in || 0) * 8) / secs / 1000 : 0;
  els.pktWire.textContent = `${bytes(dec.bytes_in)} · ${kbits.toFixed(1)} kbit/s`;

  /* by type --------------------------------------------------------------- */
  // Falls back to the pipeline's own CSI tally when talking to a server that
  // predates the per-type counters, so the panel degrades to "fewer numbers"
  // rather than a row of dashes.
  const csi = dec.csi_frames !== undefined ? dec.csi_frames : arrived;
  els.pktTypes.innerHTML = [
    cell('csi', num(csi)),
    cell('status', num(dec.status_frames)),
    cell('environment', num(dec.env_frames)),
    cell('log', num(dec.log_frames)),
    cell('total ok', num(dec.frames_ok)),
  ].join('');

  /* integrity ------------------------------------------------------------- */
  const warnIf = (v) => (v > 0 ? 'warn' : '');
  els.pktIntegrity.innerHTML = [
    cell('crc errors', num(dec.crc_errors), warnIf(dec.crc_errors || 0)),
    cell('cobs errors', num(dec.cobs_errors), warnIf(dec.cobs_errors || 0)),
    cell('short frames', num(dec.short_frames), warnIf(dec.short_frames || 0)),
    cell('unknown type', num(dec.unknown_types), warnIf(dec.unknown_types || 0)),
    cell('resyncs', num(dec.resyncs), warnIf(dec.resyncs || 0)),
  ].join('');

  /* radio ----------------------------------------------------------------- */
  // Hidden entirely over USB rather than shown as zeros: a cable has no
  // fragments and no session key, so those counters would be meaningless.
  els.pktRadioGroup.hidden = !reasm;
  if (reasm) {
    const cells = [
      cell('frames rebuilt', num(reasm.completed)),
      cell('fragment loss', num(reasm.dropped), warnIf(reasm.dropped || 0)),
      cell('duplicate frags', num(reasm.duplicates)),
    ];
    // Channel and frequency as separate cells: ".cell b" is nowrap with an
    // ellipsis, and "80 · 2480 MHz" is wider than a minimum-width column, so
    // combining them truncated the frequency to "2480 M...".
    if (radio.channel !== undefined) {
      cells.push(cell('channel', num(radio.channel)));
    }
    if (radio.frequency_mhz) {
      cells.push(cell('frequency', `${radio.frequency_mhz} MHz`));
    }
    if (crypto) {
      cells.push(cell('decrypted', num(crypto.decrypted)));
      cells.push(cell('replays blocked', num(crypto.replays), warnIf(crypto.replays || 0)));
      cells.push(cell('key resyncs', num(crypto.resyncs), warnIf(crypto.resyncs || 0)));
    } else {
      cells.push(cell('encryption', 'off', 'warn'));
    }
    els.pktRadio.innerHTML = cells.join('');
  }

  /* endpoints ------------------------------------------------------------- */
  // The node's counters are the only view of what was sent rather than what
  // survived, so a discrepancy here localises the fault to the air rather than
  // to the node or the Pi.
  els.pktEndpoints.innerHTML = [
    node ? cell('node captured', num(node.csi_count)) : '',
    node ? cell('node queue drops', num(node.dropped), warnIf(node.dropped || 0)) : '',
    cell('pipeline used', num(stats.frames_used)),
    cell('pipeline rejected', num(stats.frames_rejected), warnIf(stats.frames_rejected || 0)),
    cell('queue depth', num(link.queued), (link.queued || 0) > 512 ? 'warn' : ''),
    cell('queue overflow', num(link.dropped_full), warnIf(link.dropped_full || 0)),
    cell('stimulus sent', num(stim.sent)),
    cell('stimulus errors', num(stim.errors), warnIf(stim.errors || 0)),
  ].join('');
}

function render(s) {
  latest = s;
  const m = s.motion || {};
  const b = Object.assign({}, held.breathing, s.breathing || {});
  if (s.full) {
    held.breathing = b;
    if (s.link) held.link = s.link;
    if (s.stimulus) held.stimulus = s.stimulus;
    if (s.node !== undefined) held.node = s.node;
    if (s.series) held.series = s.series;
    // Null here means "no environment frame has ever arrived", which is a
    // real state -- a build with no sensors wired.  Only a non-null payload
    // replaces what is held, so a momentary gap does not blank the panel.
    if (s.env) { held.env = s.env; renderEnv(s.env); }
  }
  const link = held.link || {};
  const node = held.node;
  const stats = s.stats || {};

  /* header ---------------------------------------------------------------- */
  const stale = stats.stale;
  const live = link.connected && stale !== null && stale !== undefined && stale < 3;
  els.linkDot.className = 'dot' + (live ? ' live' : (link.connected ? '' : ' bad'));
  els.linkName.textContent = link.name + (link.connected ? '' : ' — disconnected')
    + (link.error ? ` (${link.error})` : '');
  // Show delivered throughput in the header: it is the number that
  // reflects what actually arrived, not the median gap between the
  // frames that happened to make it.
  els.statRate.textContent = fmt(stats.throughput_hz != null ? stats.throughput_hz : m.sample_rate, 0);
  els.statRssi.textContent = node ? node.rssi : (m.rssi || '–');
  els.statNode.textContent = node ? (node.ip || '–') : '–';

  /* hero ------------------------------------------------------------------ */
  if (m.calibrating) {
    els.stateLabel.textContent = 'CALIBRATING';
    els.stateLabel.className = 'state';
    els.stateSub.textContent =
      `establishing noise floor — ${Math.round((m.calibration_progress || 0) * 100)}%`;
  } else {
    const [label, sub] = STATE_TEXT[m.activity] || ['—', ''];
    els.stateLabel.textContent = label;
    els.stateLabel.className = 'state ' + (m.activity || '');
    els.stateSub.textContent = sub;
  }

  const mdb = m.motion_db || 0, vdb = m.vital_db || 0;
  els.motionDb.textContent = `${mdb >= 0 ? '+' : ''}${fmt(mdb)} dB`;
  els.vitalDb.textContent = `${vdb >= 0 ? '+' : ''}${fmt(vdb)} dB`;
  // 20 dB is a strong, unambiguous signal; scale the bar to that.
  els.motionBar.style.width = Math.max(0, Math.min(100, (mdb / 20) * 100)) + '%';
  els.vitalBar.style.width = Math.max(0, Math.min(100, (vdb / 20) * 100)) + '%';

  /* waterfall ------------------------------------------------------------- */
  if (s.waterfall && s.waterfall.length) {
    waterfall.pushMany(s.waterfall);
    if (surface.ok) surface.pushMany(s.waterfall);
  }

  /* activity trace -------------------------------------------------------- */
  if (s.series && s.series.length) trace.draw(s.series);

  /* respiration ----------------------------------------------------------- */
  const valid = !!b.valid;
  els.bpmValue.textContent = valid ? fmt(b.bpm, 1) : '––';
  els.bpmValue.className = 'bpm-value' + (valid ? ' live' : '');
  els.bpmConf.style.width = Math.round((b.confidence || 0) * 100) + '%';
  els.breathReason.textContent = b.reason || '–';
  // Always draw: the idle baseline is meaningful, and skipping the call would
  // leave whatever was last painted frozen on screen.
  waveform.draw(valid ? b.waveform : null, valid);
  spectrum.draw(valid ? b.spectrum : null, b.spectrum_bpm, b.bpm, valid);

  /* sub-GHz band ---------------------------------------------------------- */
  if (s.full && s.spectrum && specFall) {
    const sp = s.spectrum;
    const wasHidden = els.specPanel.hidden;
    els.specPanel.hidden = !sp.available;
    if (sp.available) {
      // The panel starts hidden, so the canvas measured 1x1 at construction and
      // CSS stretched that single pixel over the whole area -- a flat colour
      // block that looks like broken data. Size it once it is actually visible.
      if (wasHidden) specFall.resize();
      if (sp.rows && sp.rows.length) specFall.pushMany(sp.rows);

      if (!specAxisDone && sp.freqs_mhz && sp.freqs_mhz.length) {
        const f = sp.freqs_mhz;
        els.specRange.textContent = `${f[0].toFixed(1)}–${f[f.length-1].toFixed(1)} MHz`;
        const mid = f[Math.floor(f.length / 2)];
        els.specAxis.innerHTML =
          `<span>${f[0].toFixed(1)}</span><span>${mid.toFixed(1)}</span>` +
          `<span>${f[f.length-1].toFixed(1)} MHz</span>`;
        specAxisDone = true;
      }

      // Strongest channel right now, whether or not it counts as an event.
      if (sp.latest_dbm && sp.latest_dbm.length) {
        let bi = 0;
        for (let i = 1; i < sp.latest_dbm.length; i++) {
          if (sp.latest_dbm[i] > sp.latest_dbm[bi]) bi = i;
        }
        els.specPeak.textContent = `${sp.latest_dbm[bi].toFixed(0)} dBm`;
        els.specPeakF.textContent = `@ ${sp.freqs_mhz[bi].toFixed(2)} MHz`;
      }

      els.specEvents.innerHTML = (sp.events && sp.events.length)
        ? sp.events.map(e =>
            `<div class="spec-ev"><b>${e.mhz.toFixed(2)} MHz</b>` +
            `<i>${e.dbm.toFixed(0)} dBm &middot; +${e.over.toFixed(0)}dB</i></div>`).join('')
        : '<span class="spec-quiet">band quiet</span>';
    }
  }

  /* diagnostics ----------------------------------------------------------- */
  const dec = link.decode || {};
  const stim = held.stimulus || {};
  const errs = (dec.crc_errors || 0) + (dec.cobs_errors || 0);
  const dropped = stats.dropped_by_seq || 0;
  els.diag.innerHTML = [
    cell('delivered', fmt(stats.throughput_hz, 1) + ' Hz'),
    cell('inter-arrival', fmt(m.sample_rate, 1) + ' Hz'),
    cell('stimulus', fmt(stim.rate_hz, 0) + ' Hz'),
    cell('target', stim.target || 'auto'),
    cell('frames', stats.frames_used || 0),
    cell('link errors', errs, errs > 0 ? 'warn' : ''),
    cell('dropped', dropped, dropped > 0 ? 'warn' : ''),
    cell('rejected', stats.frames_rejected || 0, (stats.frames_rejected || 0) > 0 ? 'warn' : ''),
    cell('stale', stale === null || stale === undefined ? '–' : fmt(stale, 1) + ' s',
         (stale > 3) ? 'bad' : ''),
    node ? cell('node mode', node.link_mode) : '',
    node ? cell('channel', node.channel) : '',
    node ? cell('node drops', node.dropped, node.dropped > 0 ? 'warn' : '') : '',
    node ? cell('heap', Math.round(node.free_heap / 1024) + ' K') : '',
    cell('uptime', fmt(stats.uptime, 0) + ' s'),
  ].join('');

  /* packets --------------------------------------------------------------- */
  renderPackets(stats, link, node, stim);

  if (s.full && s.recording !== recording) {
    recording = s.recording;
    els.btnRecord.classList.toggle('on', recording);
    els.btnRecord.textContent = recording ? '● REC' : 'REC';
  }
}

/* ---------------------------------------------------------------- websocket */

let ws = null;
let retry = 0;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => { retry = 0; };
  ws.onmessage = (ev) => {
    try { render(JSON.parse(ev.data)); }
    catch (e) { /* a single malformed frame must not kill the stream */ }
  };
  ws.onclose = () => {
    els.linkDot.className = 'dot bad';
    els.linkName.textContent = 'reconnecting…';
    // Exponential backoff, capped: the Pi may be restarting the service.
    retry = Math.min(retry + 1, 6);
    setTimeout(connect, 400 * Math.pow(1.7, retry));
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

/* ------------------------------------------------------------------ actions */

els.btnRecord.addEventListener('click', async () => {
  const path = recording ? '/api/record/stop' : '/api/record/start';
  try { await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); }
  catch (e) {}
});

els.btnReset.addEventListener('click', async () => {
  try {
    await fetch('/api/reset', { method: 'POST' });
    waterfall.clear();
    if (surface.ok) surface.clear();
  } catch (e) {}
});

connect();
