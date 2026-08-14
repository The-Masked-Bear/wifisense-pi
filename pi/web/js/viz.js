/* Rendering primitives: colour maps, the scrolling waterfall, and the traces.
   No external libraries -- the Pi may have no internet, and a CDN fetch would
   simply fail behind the offline deployment this is built for. */

'use strict';

/* ---------------------------------------------------------------- colour maps

   Anchor points sampled from the canonical matplotlib maps.  These are all
   perceptually uniform, which matters here: a naive rainbow ramp invents
   banding that reads as structure in the data that is not actually present. */

const RAMPS = {
  viridis: [
    [68,1,84],[72,40,120],[62,74,137],[49,104,142],[38,130,142],[31,158,137],
    [53,183,121],[109,205,89],[180,222,44],[226,228,24],[253,231,37],
  ],
  inferno: [
    [0,0,4],[22,11,57],[66,10,104],[106,23,110],[147,38,103],[188,55,84],
    [221,81,58],[243,120,25],[252,165,10],[246,215,70],[252,255,164],
  ],
  turbo: [
    [48,18,59],[70,107,227],[62,155,254],[24,214,203],[70,248,131],
    [163,255,53],[225,220,55],[253,165,49],[239,89,17],[196,37,2],[122,4,3],
  ],
};

/* Precomputed 256-entry lookup, built once. Interpolating per pixel would cost
   ~20k operations per waterfall column on a Pi 4. */
function buildLUT(name) {
  const anchors = RAMPS[name] || RAMPS.viridis;
  const lut = new Uint8ClampedArray(256 * 3);
  const seg = anchors.length - 1;
  for (let i = 0; i < 256; i++) {
    const x = (i / 255) * seg;
    const j = Math.min(Math.floor(x), seg - 1);
    const f = x - j;
    const a = anchors[j], b = anchors[j + 1];
    lut[i * 3]     = a[0] + (b[0] - a[0]) * f;
    lut[i * 3 + 1] = a[1] + (b[1] - a[1]) * f;
    lut[i * 3 + 2] = a[2] + (b[2] - a[2]) * f;
  }
  return lut;
}

const LUTS = { viridis: buildLUT('viridis'), inferno: buildLUT('inferno'), turbo: buildLUT('turbo') };

/* Bitmap size a canvas should have, or null when it has no layout box.

   A hidden canvas measures 0x0, and rounding that up to a 1x1 bitmap is worse
   than skipping the resize: Waterfall.resize() rescales the history it already
   holds into the new size, so one resize behind a `hidden` attribute squashes a
   minute of data into a single pixel, and the next reveal stretches that pixel
   back over the whole panel -- a flat colour block that reads as broken data.
   Every resize below leaves an unsized canvas alone and keeps its last good
   bitmap; whichever view becomes visible re-fits itself on reveal. */
function fitCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1) return null;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  return { w: Math.floor(rect.width * dpr), h: Math.floor(rect.height * dpr), dpr };
}

/* ------------------------------------------------------------------ waterfall

   Time flows left to right; sub-carrier frequency runs up the Y axis.

   The scroll is done by blitting the canvas onto itself shifted one column
   left, then drawing only the newest column. Redrawing the whole history each
   frame would be ~600x more pixel work and drops the Pi to single-digit fps. */

class Waterfall {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d', { alpha: false });
    this.ctx.imageSmoothingEnabled = false;
    this.ramp = opts.ramp || 'viridis';
    this.bins = opts.bins || 32;
    // Pixels advanced per sample.  CSI arrives ~25x/second and fills a wide
    // canvas in under a minute at 1 px; the sub-GHz sweep arrives ~1.2x/second
    // and would take a quarter of an hour. Wider columns make slow data fill
    // the panel at a sensible pace and read as the coarser measurement it is.
    this.colWidth = Math.max(1, opts.columnWidth || 1);
    this.column = this.ctx.createImageData(1, 1);
    this._sized = false;
    this.resize();
  }

  setRamp(name) { if (LUTS[name]) this.ramp = name; }

  resize() {
    const fit = fitCanvas(this.canvas);
    if (!fit) return;
    const { w, h } = fit;
    if (w === this.canvas.width && h === this.canvas.height) return;

    // Preserve what is already drawn across a resize so the history does not
    // blank out when the phone rotates or a panel reflows.
    let prev = null;
    if (this._sized && this.canvas.width > 0 && this.canvas.height > 0) {
      prev = document.createElement('canvas');
      prev.width = this.canvas.width; prev.height = this.canvas.height;
      prev.getContext('2d').drawImage(this.canvas, 0, 0);
    }
    this.canvas.width = w; this.canvas.height = h;
    this.ctx.imageSmoothingEnabled = false;
    this.ctx.fillStyle = '#080b12';
    this.ctx.fillRect(0, 0, w, h);
    if (prev) this.ctx.drawImage(prev, 0, 0, w, h);
    this.column = this.ctx.createImageData(1, h);
    this._sized = true;
  }

  /** Append one column. `row` is `bins` values in 0..255, low frequency first. */
  push(row) {
    const { ctx, canvas } = this;
    const h = canvas.height, w = canvas.width;
    if (!w || !h || !row || !row.length) return;

    ctx.drawImage(canvas, -this.colWidth, 0);

    const lut = LUTS[this.ramp];
    const data = this.column.data;
    const n = row.length;
    for (let y = 0; y < h; y++) {
      // Flip so low sub-carriers sit at the bottom, matching the axis label.
      const idx = Math.min(n - 1, Math.floor(((h - 1 - y) / h) * n));
      const v = row[idx] & 255;
      const o = v * 3, p = y * 4;
      data[p] = lut[o]; data[p + 1] = lut[o + 1]; data[p + 2] = lut[o + 2]; data[p + 3] = 255;
    }
    ctx.putImageData(this.column, w - this.colWidth, 0);
    // putImageData writes a single 1-px column; repeat it to fill the width.
    for (let x = 1; x < this.colWidth; x++) {
      ctx.putImageData(this.column, w - this.colWidth + x, 0);
    }
  }

  pushMany(rows) { for (const r of rows) this.push(r); }

  clear() {
    this.ctx.fillStyle = '#080b12';
    this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
  }
}

/* --------------------------------------------------------------- trace chart

   A dual-series scrolling line chart for motion and vitals, in dB. Drawn from
   scratch each frame: at ~240 points that is trivial, and it avoids pulling in
   a charting library that would need vendoring. */

class Trace {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.min = opts.min !== undefined ? opts.min : -5;
    this.max = opts.max !== undefined ? opts.max : 30;
    this.series = opts.series || [
      { key: 'm', color: '#4dd4c4', fill: 'rgba(77,212,196,0.13)', label: 'motion' },
      { key: 'p', color: '#c88bff', fill: 'rgba(200,139,255,0.10)', label: 'vitals' },
    ];
    this.thresholds = opts.thresholds || [];
    this.resize();
  }

  resize() {
    const fit = fitCanvas(this.canvas);
    if (!fit) return;
    this.canvas.width = fit.w;
    this.canvas.height = fit.h;
    this.dpr = fit.dpr;
  }

  draw(points) {
    const { ctx, canvas } = this;
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (!points || points.length < 2) return;

    const pad = 4 * (this.dpr || 1);
    const span = this.max - this.min;
    const yOf = (v) => h - pad - ((Math.max(this.min, Math.min(this.max, v)) - this.min) / span) * (h - pad * 2);
    const xOf = (i) => (i / (points.length - 1)) * w;

    // Horizontal reference lines at the detection thresholds, so the reading
    // can be judged against the decision the system is actually making.
    ctx.setLineDash([3 * this.dpr, 4 * this.dpr]);
    ctx.lineWidth = 1;
    for (const t of this.thresholds) {
      ctx.strokeStyle = t.color || 'rgba(255,255,255,0.13)';
      ctx.beginPath(); ctx.moveTo(0, yOf(t.value)); ctx.lineTo(w, yOf(t.value)); ctx.stroke();
    }
    ctx.setLineDash([]);

    for (const s of this.series) {
      ctx.beginPath();
      ctx.moveTo(xOf(0), yOf(points[0][s.key] || 0));
      for (let i = 1; i < points.length; i++) ctx.lineTo(xOf(i), yOf(points[i][s.key] || 0));

      if (s.fill) {
        ctx.save();
        ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
        ctx.fillStyle = s.fill; ctx.fill();
        ctx.restore();
        ctx.beginPath();
        ctx.moveTo(xOf(0), yOf(points[0][s.key] || 0));
        for (let i = 1; i < points.length; i++) ctx.lineTo(xOf(i), yOf(points[i][s.key] || 0));
      }
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 1.6 * (this.dpr || 1);
      ctx.lineJoin = 'round';
      ctx.stroke();
    }
  }
}

/* ------------------------------------------------------------ breathing trace */

class Waveform {
  constructor(canvas, color = '#7fd1ff') {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.color = color;
    this.resize();
  }

  resize() {
    const fit = fitCanvas(this.canvas);
    if (!fit) return;
    this.canvas.width = fit.w;
    this.canvas.height = fit.h;
    this.dpr = fit.dpr;
  }

  draw(values, active) {
    const { ctx, canvas } = this;
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    /* Respiration only resolves on a still subject, so "no waveform" is a
       normal operating state, not a fault. Draw a flat baseline for it -- an
       empty panel reads as broken. */
    if (!values || values.length < 2) {
      ctx.save();
      ctx.setLineDash([4 * (this.dpr || 1), 5 * (this.dpr || 1)]);
      ctx.strokeStyle = 'rgba(124,138,163,0.30)';
      ctx.lineWidth = 1 * (this.dpr || 1);
      ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
      ctx.restore();
      return;
    }

    const mid = h / 2, amp = h * 0.40;
    ctx.beginPath();
    for (let i = 0; i < values.length; i++) {
      const x = (i / (values.length - 1)) * w;
      const y = mid - values[i] * amp;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.strokeStyle = active ? this.color : 'rgba(140,155,180,0.35)';
    ctx.lineWidth = 1.8 * (this.dpr || 1);
    ctx.lineJoin = 'round';
    if (active) { ctx.shadowColor = this.color; ctx.shadowBlur = 8 * (this.dpr || 1); }
    ctx.stroke();
    ctx.shadowBlur = 0;
  }
}

/* ------------------------------------------------------- respiration spectrum */

class Spectrum {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.resize();
  }

  resize() {
    const fit = fitCanvas(this.canvas);
    if (!fit) return;
    this.canvas.width = fit.w;
    this.canvas.height = fit.h;
    this.dpr = fit.dpr;
  }

  draw(mags, bpms, peakBpm, active) {
    const { ctx, canvas } = this;
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (!mags || !mags.length) {
      ctx.fillStyle = 'rgba(124,138,163,0.13)';
      ctx.fillRect(0, h - 1, w, 1);
      return;
    }

    const bw = w / mags.length;
    for (let i = 0; i < mags.length; i++) {
      const v = Math.max(0, Math.min(1, mags[i]));
      const bh = v * h;
      // Highlight the bin the estimate came from, so a spurious peak is
      // visible as such rather than hidden behind a confident number.
      const isPeak = active && bpms && Math.abs(bpms[i] - peakBpm) < 1.2;
      ctx.fillStyle = isPeak ? 'rgba(127,209,255,0.95)'
                             : `rgba(127,209,255,${0.18 + v * 0.42})`;
      ctx.fillRect(i * bw, h - bh, Math.max(1, bw - 1), bh);
    }
  }
}

/* -------------------------------------------------------------- sparkline

   A single auto-ranged series with no axes, for the environment cards.

   Auto-ranging is the whole point: indoor temperature moves over a degree in
   an hour and pressure over a couple of hectopascals in a day, so any fixed
   scale wide enough to be safe renders both as a flat line. The trade-off is
   that the vertical scale is meaningless in isolation -- which is why the
   card always shows the number and the span in text beside it. */

class Sparkline {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.color = opts.color || '#7fd1ff';
    this.fill = opts.fill || 'rgba(127,209,255,0.14)';
    this.resize();
  }

  resize() {
    const fit = fitCanvas(this.canvas);
    if (!fit) return;
    this.canvas.width = fit.w;
    this.canvas.height = fit.h;
    this.dpr = fit.dpr;
  }

  /* Recolour at runtime, so a series whose meaning changes with its value --
     the air quality index and its category bands -- can carry that colour
     rather than sitting under a number painted a different one. */
  setColor(line, fill) {
    if (line) this.color = line;
    if (fill) this.fill = fill;
  }

  /* `values` may contain nulls where that sensor did not answer; those become
     breaks in the line rather than a plunge to zero, which would otherwise
     look like a dramatic reading instead of a missing one. */
  draw(values) {
    const { ctx, canvas } = this;
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (!values || values.length < 2) return;

    let lo = Infinity, hi = -Infinity, n = 0;
    for (const v of values) {
      if (v === null || v === undefined || Number.isNaN(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
      n++;
    }
    if (n < 2) return;

    // A perfectly flat series has zero span, which would divide by zero and
    // then draw nothing.  Give it an arbitrary band and centre the line.
    let span = hi - lo;
    if (!(span > 0)) { span = 1; lo -= 0.5; }
    const pad = 3 * (this.dpr || 1);
    const yOf = (v) => h - pad - ((v - lo) / span) * (h - pad * 2);
    const xOf = (i) => (i / (values.length - 1)) * w;

    // Fill first, as a separate pass over each contiguous run, then the line
    // on top.  Filling per-run keeps a gap from being bridged by the baseline.
    ctx.lineWidth = 1.5 * (this.dpr || 1);
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    let i = 0;
    while (i < values.length) {
      while (i < values.length && (values[i] === null || values[i] === undefined)) i++;
      const start = i;
      while (i < values.length && values[i] !== null && values[i] !== undefined) i++;
      const end = i;  // exclusive
      if (end - start < 2) continue;

      ctx.beginPath();
      ctx.moveTo(xOf(start), yOf(values[start]));
      for (let j = start + 1; j < end; j++) ctx.lineTo(xOf(j), yOf(values[j]));
      ctx.lineTo(xOf(end - 1), h);
      ctx.lineTo(xOf(start), h);
      ctx.closePath();
      ctx.fillStyle = this.fill;
      ctx.fill();

      ctx.beginPath();
      ctx.moveTo(xOf(start), yOf(values[start]));
      for (let j = start + 1; j < end; j++) ctx.lineTo(xOf(j), yOf(values[j]));
      ctx.strokeStyle = this.color;
      ctx.stroke();
    }
  }
}

window.Viz = { Waterfall, Trace, Waveform, Spectrum, Sparkline, RAMPS };
