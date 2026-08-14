/* Live 3D surface of the channel response.

   IMPORTANT, because the picture invites the wrong reading: the three axes are
   frequency, time and signal amplitude.  None of them is a spatial axis.  This
   is not a map of the room and no point on it corresponds to a location.

   With one transmitter and one receiver there is no angle information at all,
   and 20 MHz of bandwidth gives a range resolution of c/2B = 7.5 m -- larger
   than most rooms.  Position is not merely noisy in this data, it is absent.
   What the surface does show is the complete measurement the sensor actually
   makes: the channel's frequency response, and how it deforms over time.  A
   person moving drags visible waves across the terrain; breathing shows as a
   slow regular swell.

   Implemented in raw WebGL rather than a library. Three.js would be ~600 KB to
   vendor for one mesh, and the Pi is expected to work with no internet.

   The height field lives in a texture that scrolls by one column per new
   sample, and the vertex shader displaces the grid by sampling it. Uploading a
   full vertex buffer each frame instead would push ~740 KB/s over the bus for
   no benefit. */

'use strict';

(function () {

/* ------------------------------------------------------------------- mat4 */

function perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
  return [f / aspect, 0, 0, 0,  0, f, 0, 0,  0, 0, (far + near) * nf, -1,  0, 0, 2 * far * near * nf, 0];
}

function lookAt(eye, center, up) {
  const [ex, ey, ez] = eye, [cx, cy, cz] = center, [ux, uy, uz] = up;
  let zx = ex - cx, zy = ey - cy, zz = ez - cz;
  let l = Math.hypot(zx, zy, zz) || 1; zx /= l; zy /= l; zz /= l;
  let xx = uy * zz - uz * zy, xy = uz * zx - ux * zz, xz = ux * zy - uy * zx;
  l = Math.hypot(xx, xy, xz) || 1; xx /= l; xy /= l; xz /= l;
  const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
  return [xx, yx, zx, 0,  xy, yy, zy, 0,  xz, yz, zz, 0,
          -(xx * ex + xy * ey + xz * ez), -(yx * ex + yy * ey + yz * ez),
          -(zx * ex + zy * ey + zz * ez), 1];
}

function mul(a, b) {
  const o = new Float32Array(16);
  for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) {
    let s = 0;
    for (let k = 0; k < 4; k++) s += a[k * 4 + j] * b[i * 4 + k];
    o[i * 4 + j] = s;
  }
  return o;
}

/* --------------------------------------------------------------- framing

   The panel is a wide, short box -- 4:1 on a desktop -- because it was sized for
   the 2D waterfall, and the fixed 0.85 rad vertical field this was drawn with got
   the worst of both: the near and far edges of the surface spilled out of the
   panel (max |ndc_y| ~= 1.09 at the default tilt, 1.28 tilted to top-down) while
   only a third of the width was ever used.

   So the field is fitted to the geometry rather than guessed. The grid is 2 x 2.4
   in model space and it spins, so bound it by the cylinder it sweeps: radius
   hypot(1, 1.2) about the vertical axis, base to peak displacement. A cylinder is
   rotation-invariant, so the framing holds still while the surface turns --
   fitting the corners themselves would pump it in and out by 20% a revolution.

   The fit is always evaluated at REF_RADIUS, never at the live radius, so zooming
   still zooms instead of being cancelled out by the fit that follows it. */

const MODEL_R = Math.hypot(1.0, 1.2);
const REF_RADIUS = 3.05;
const FILL = 0.94;                 // a little air around the silhouette
const TARGET = [0, 0.10, 0];

const RIM = [];
for (let i = 0; i < 24; i++) {
  const a = (i / 24) * Math.PI * 2;
  RIM.push([Math.cos(a) * MODEL_R, Math.sin(a) * MODEL_R]);
}

/* ----------------------------------------------------------------- shaders */

const VERT = `
precision highp float;
attribute vec2 aGrid;              // (bin, time) each normalised 0..1
uniform sampler2D uHeight;
uniform float uOffset;             // scroll position of the ring buffer
uniform float uAmp;
uniform vec2  uTexel;              // 1/width, 1/height of the height texture
uniform mat4  uMVP;
varying float vH;
varying vec3  vNormal;
varying float vFade;

// The scroll wraps here, in fract(), rather than in the sampler: the height
// texture is not power-of-two wide and WebGL 1 will not wrap one. See
// _buildTextures.
float sampleH(float t, float b) {
  return texture2D(uHeight, vec2(fract(t), b)).r;
}

void main() {
  float t = aGrid.y + uOffset;
  float h = sampleH(t, aGrid.x);

  // Central differences for a surface normal. Four extra fetches per vertex is
  // cheap and lighting is what makes the terrain legible rather than a flat
  // coloured sheet.
  float hL = sampleH(t, max(aGrid.x - uTexel.y, 0.0));
  float hR = sampleH(t, min(aGrid.x + uTexel.y, 1.0));
  float hD = sampleH(t - uTexel.x, aGrid.x);
  float hU = sampleH(t + uTexel.x, aGrid.x);
  vNormal = normalize(vec3((hL - hR) * uAmp, 0.16, (hD - hU) * uAmp));

  vH = h;
  // Oldest samples fade out, so the leading edge reads as "now".
  vFade = smoothstep(0.0, 0.22, aGrid.y);

  vec3 pos = vec3((aGrid.x - 0.5) * 2.0, h * uAmp, (aGrid.y - 0.5) * 2.4);
  gl_Position = uMVP * vec4(pos, 1.0);
}`;

const FRAG = `
precision highp float;
uniform sampler2D uRamp;
varying float vH;
varying vec3  vNormal;
varying float vFade;

void main() {
  vec3 base = texture2D(uRamp, vec2(clamp(vH, 0.0, 1.0), 0.5)).rgb;
  vec3 L = normalize(vec3(-0.45, 0.82, 0.36));
  float lambert = max(dot(normalize(vNormal), L), 0.0);
  vec3 col = base * (0.55 + 0.62 * lambert);
  col = mix(vec3(0.031, 0.043, 0.071), col, vFade);   // fade into the panel bg
  gl_FragColor = vec4(col, 1.0);
}`;

/* ---------------------------------------------------------------- Surface3D */

class Surface3D {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.bins = opts.bins || 32;
    this.depth = opts.depth || 256;          // time steps retained
    this.amp = opts.amp || 0.55;
    this.ok = false;

    const gl = canvas.getContext('webgl', { antialias: true, alpha: false })
            || canvas.getContext('experimental-webgl', { antialias: true, alpha: false });
    if (!gl) return;
    // The vertex shader reads the height texture, which needs vertex texture
    // fetch. Universally supported on anything recent, but check rather than
    // render a flat plane and leave the user wondering.
    if (gl.getParameter(gl.MAX_VERTEX_TEXTURE_IMAGE_UNITS) < 1) return;
    this.gl = gl;

    this.prog = this._program(VERT, FRAG);
    if (!this.prog) return;

    this._buildGrid();
    this._buildTextures(opts.ramp || 'viridis');

    this.head = 0;
    this.rotation = -0.62;
    this.pitch = 0.52;
    this.radius = 3.05;
    this.dragging = false;
    this.autoRotate = true;
    this._attachControls();
    this._buildOverlay();

    // A WebGL context can be lost at any time -- GPU reset, driver recovery, a
    // backgrounded tab, or memory pressure. Left unhandled the canvas simply
    // goes blank and every draw call silently no-ops, which reads as "the 3D
    // view is broken" rather than "the GPU went away". Report it so the app can
    // fall back to the 2D waterfall, which needs no GPU at all.
    canvas.addEventListener('webglcontextlost', (e) => {
      e.preventDefault();              // required, or the context never returns
      this.ok = false;
      if (typeof this.onContextLost === 'function') this.onContextLost();
    }, false);
    canvas.addEventListener('webglcontextrestored', () => {
      try {
        this.prog = this._program(VERT, FRAG);
        if (!this.prog) return;
        this._buildGrid();
        this._buildTextures(this.ramp);
        this.gl.enable(this.gl.DEPTH_TEST);
        this.gl.clearColor(0.031, 0.043, 0.071, 1);
        this.head = 0;
        this.resize();
        this.ok = true;
        if (typeof this.onContextRestored === 'function') this.onContextRestored();
      } catch (err) { this.ok = false; }
    }, false);

    this.ramp = opts.ramp || 'viridis';
    gl.enable(gl.DEPTH_TEST);
    gl.clearColor(0.031, 0.043, 0.071, 1);
    this.resize();
    this.ok = true;
  }

  _program(vsrc, fsrc) {
    const gl = this.gl;
    const compile = (type, src) => {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src); gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        console.warn('shader:', gl.getShaderInfoLog(sh)); return null;
      }
      return sh;
    };
    const vs = compile(gl.VERTEX_SHADER, vsrc), fs = compile(gl.FRAGMENT_SHADER, fsrc);
    if (!vs || !fs) return null;
    const p = gl.createProgram();
    gl.attachShader(p, vs); gl.attachShader(p, fs); gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      console.warn('link:', gl.getProgramInfoLog(p)); return null;
    }
    this.loc = {
      aGrid: gl.getAttribLocation(p, 'aGrid'),
      uHeight: gl.getUniformLocation(p, 'uHeight'),
      uRamp: gl.getUniformLocation(p, 'uRamp'),
      uOffset: gl.getUniformLocation(p, 'uOffset'),
      uAmp: gl.getUniformLocation(p, 'uAmp'),
      uTexel: gl.getUniformLocation(p, 'uTexel'),
      uMVP: gl.getUniformLocation(p, 'uMVP'),
    };
    return p;
  }

  _buildGrid() {
    const gl = this.gl, W = this.bins, D = this.depth;
    const verts = new Float32Array(W * D * 2);
    let k = 0;
    for (let j = 0; j < D; j++)
      for (let i = 0; i < W; i++) {
        verts[k++] = i / (W - 1);
        verts[k++] = j / (D - 1);
      }
    this.vbo = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.vbo);
    gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);

    // 32 x 256 grid exceeds 65535 vertices? 8192 -- fine for 16-bit indices.
    const idx = new Uint16Array((W - 1) * (D - 1) * 6);
    k = 0;
    for (let j = 0; j < D - 1; j++)
      for (let i = 0; i < W - 1; i++) {
        const a = j * W + i, b = a + 1, c = a + W, d = c + 1;
        idx[k++] = a; idx[k++] = c; idx[k++] = b;
        idx[k++] = b; idx[k++] = c; idx[k++] = d;
      }
    this.ibo = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.ibo);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
    this.indexCount = idx.length;
  }

  _buildTextures(rampName) {
    const gl = this.gl;
    // Rows of the height field are uploaded one pixel wide, i.e. 1 byte per
    // row.  WebGL's default UNPACK_ALIGNMENT of 4 pads every row to 4 bytes and
    // then rejects the buffer as too small -- so the scroll column must be
    // uploaded with byte alignment.
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);

    // Height field: width = time, height = sub-carrier bin.
    this.heightTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.heightTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, this.depth, this.bins, 0,
                  gl.LUMINANCE, gl.UNSIGNED_BYTE, new Uint8Array(this.depth * this.bins));
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    // Both axes clamp -- including time, even though time is a ring buffer and
    // GL_REPEAT is exactly what a ring buffer wants. WebGL 1 only wraps
    // power-of-two textures, and `depth` is 1500 to match the column history the
    // server keeps. A non-power-of-two texture with GL_REPEAT is *incomplete*:
    // every fetch returns 0, with no GL error and nothing on the console, so the
    // whole surface rendered as a flat sheet in the ramp's first colour -- the 3D
    // view looked broken while the data behind it was fine. Clamping costs only
    // the interpolation across the seam, and that seam joins the newest column
    // to the oldest one, which vFade has already faded into the background.
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    this.rampTex = gl.createTexture();
    this.setRamp(rampName);
    this._col = new Uint8Array(this.bins);
  }

  setRamp(name) {
    const gl = this.gl;
    if (!gl) return;
    this.ramp = name;
    // Reuse the 2D view's ramps so both representations agree on colour.
    const anchors = (window.Viz && window.Viz.RAMPS && window.Viz.RAMPS[name])
                  || (window.Viz && window.Viz.RAMPS && window.Viz.RAMPS.viridis);
    if (!anchors) return;
    const lut = new Uint8Array(256 * 3);
    const seg = anchors.length - 1;
    for (let i = 0; i < 256; i++) {
      const x = (i / 255) * seg, j = Math.min(Math.floor(x), seg - 1), f = x - j;
      const a = anchors[j], b = anchors[j + 1];
      lut[i * 3] = a[0] + (b[0] - a[0]) * f;
      lut[i * 3 + 1] = a[1] + (b[1] - a[1]) * f;
      lut[i * 3 + 2] = a[2] + (b[2] - a[2]) * f;
    }
    gl.bindTexture(gl.TEXTURE_2D, this.rampTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, 256, 1, 0, gl.RGB, gl.UNSIGNED_BYTE, lut);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  }

  /** Append one sample: `row` is `bins` values in 0..255, low frequency first. */
  push(row) {
    if (!this.ok || !row || !row.length) return;
    const gl = this.gl, n = this.bins;
    for (let i = 0; i < n; i++) {
      const idx = Math.min(row.length - 1, Math.floor((i / n) * row.length));
      this._col[i] = row[idx] & 255;
    }
    // One column, not the whole field: this is the entire reason the height
    // field lives in a texture.
    gl.bindTexture(gl.TEXTURE_2D, this.heightTex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, this.head, 0, 1, n,
                     gl.LUMINANCE, gl.UNSIGNED_BYTE, this._col);
    this.head = (this.head + 1) % this.depth;
  }

  pushMany(rows) { for (const r of rows) this.push(r); }

  resize() {
    if (!this.gl) return;
    // Nothing to fit while the canvas is hidden: a 0x0 rect would become a 1x1
    // framebuffer, and every frame drawn into it is one stretched pixel.
    // setVisible() re-fits the view it reveals.
    const rect = this.canvas.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.floor(rect.width * dpr);
    const h = Math.floor(rect.height * dpr);
    if (w !== this.canvas.width || h !== this.canvas.height) {
      this.canvas.width = w; this.canvas.height = h;
    }
    this.gl.viewport(0, 0, w, h);
  }

  /** Show or hide the whole 3D view.

      The overlay is a sibling of the canvas rather than a child -- canvases have
      no children -- so hiding the canvas alone left the axis labels and the
      RESET button floating over the 2D waterfall. */
  setVisible(visible) {
    this.canvas.hidden = !visible;
    if (this.overlay) this.overlay.style.display = visible ? 'flex' : 'none';
    if (visible) this.resize();
  }

  _buildOverlay() {
    this.overlay = document.createElement('div');
    this.overlay.className = 'surface3d-overlay';
    // Pointer-events none on the container so we can still interact with the canvas underneath,
    // except for the reset button.
    Object.assign(this.overlay.style, {
      position: 'absolute', top: 0, left: 0, width: '100%', height: '100%',
      pointerEvents: 'none', display: 'flex', flexDirection: 'column',
      justifyContent: 'space-between', padding: '10px', boxSizing: 'border-box',
      color: 'rgba(255, 255, 255, 0.7)', fontFamily: 'ui-monospace, monospace', fontSize: '10px'
    });

    const seconds = Math.round(this.depth / 25.0); // 1500 depth at 25Hz = 60s
    this.overlay.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: flex-start;">
        <div>
          <div><b>FREQUENCY</b> (sub-carrier bin) &rarr;</div>
          <div style="margin-top:4px"><b>AMPLITUDE</b> &uarr;</div>
        </div>
        <button class="ghost" style="pointer-events: auto; background: rgba(0,0,0,0.5);" title="Reset view">RESET</button>
      </div>
      <div style="display: flex; justify-content: space-between; align-items: flex-end;">
        <div>&larr; <b>TIME</b> (${seconds}s history)</div>
        <div style="text-align: right;">
          <div><b style="color: #4ade8a">NOW</b> &rarr;</div>
          <div style="margin-top:4px; font-size:9px; opacity:0.7">drag: rotate &middot; pinch/scroll: zoom &middot; dbl-click: spin</div>
        </div>
      </div>
    `;
    
    // Positioned against a wrapper rather than the panel, so it tracks the
    // canvas and not the whole section. The wrapper collapses to nothing when
    // the canvas is hidden, which is why setVisible() takes the overlay down
    // with it rather than letting it overflow a zero-height box.
    const container = document.createElement('div');
    container.style.position = 'relative';
    container.style.width = '100%';
    this.canvas.parentNode.insertBefore(container, this.canvas);
    container.appendChild(this.canvas);
    container.appendChild(this.overlay);


    const btn = this.overlay.querySelector('button');
    btn.addEventListener('click', () => {
      this.rotation = -0.62;
      this.pitch = 0.52;
      this.radius = 3.05;
      this.autoRotate = true;
    });
  }

  _attachControls() {
    const c = this.canvas;
    let lx = 0, ly = 0;
    const pointers = new Map();

    const down = (e) => {
      c.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      this.dragging = true;
      this.autoRotate = false;
    };

    const move = (e) => {
      if (!pointers.has(e.pointerId)) return;

      if (pointers.size === 1) {
        const last = pointers.get(e.pointerId);
        this.rotation += (e.clientX - last.x) * 0.008;
        this.pitch = Math.max(0.06, Math.min(1.35, this.pitch + (e.clientY - last.y) * 0.006));
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      } else if (pointers.size === 2) {
        // Pinch zoom
        const pts = Array.from(pointers.values());
        const d1 = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
        
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        const ptsNew = Array.from(pointers.values());
        const d2 = Math.hypot(ptsNew[0].x - ptsNew[1].x, ptsNew[0].y - ptsNew[1].y);
        
        this.radius = Math.max(1.0, Math.min(10.0, this.radius - (d2 - d1) * 0.01));
      }
    };

    const up = (e) => {
      pointers.delete(e.pointerId);
      if (pointers.size === 0) this.dragging = false;
    };

    c.addEventListener('pointerdown', down);
    c.addEventListener('pointermove', move);
    c.addEventListener('pointerup', up);
    c.addEventListener('pointercancel', up);
    c.addEventListener('pointerleave', up);

    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      this.radius = Math.max(1.0, Math.min(10.0, this.radius + e.deltaY * 0.002));
    }, { passive: false });

    c.addEventListener('dblclick', () => { this.autoRotate = !this.autoRotate; });
  }

  _eye(r) {
    return [Math.sin(this.rotation) * r * Math.cos(this.pitch),
            Math.sin(this.pitch) * r + 0.30,
            Math.cos(this.rotation) * r * Math.cos(this.pitch)];
  }

  /** Vertical field of view that just contains the surface at this tilt and
      panel shape. 48 silhouette points is nothing next to a 48k-vertex mesh, so
      it is recomputed per frame and needs no invalidation. */
  _fovy(aspect) {
    const v = lookAt(this._eye(REF_RADIUS), TARGET, [0, 1, 0]);
    let t = 0;
    for (const [x, z] of RIM) {
      for (let k = 0; k < 2; k++) {
        const y = k ? this.amp : 0;
        const X = v[0] * x + v[4] * y + v[8] * z + v[12];
        const Y = v[1] * x + v[5] * y + v[9] * z + v[13];
        const Z = v[2] * x + v[6] * y + v[10] * z + v[14];
        const d = Math.max(0.05, -Z);
        // Both requirements expressed as tan(fovy/2): vertically direct,
        // horizontally divided out by the aspect ratio.
        t = Math.max(t, Math.abs(Y) / d, Math.abs(X) / (d * aspect));
      }
    }
    // Clamped so a canvas caught mid-layout cannot ask for a pinhole or a fisheye.
    return 2 * Math.atan(Math.min(1.2, Math.max(0.12, t / FILL)));
  }

  render() {
    if (!this.ok || this.canvas.hidden) return;

    const gl = this.gl;
    if (this.autoRotate && !this.dragging) this.rotation += 0.0016;

    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(this.prog);

    const aspect = this.canvas.width / Math.max(1, this.canvas.height);
    const mvp = mul(perspective(this._fovy(aspect), aspect, 0.1, 40),
                    lookAt(this._eye(this.radius), TARGET, [0, 1, 0]));

    gl.bindBuffer(gl.ARRAY_BUFFER, this.vbo);
    gl.enableVertexAttribArray(this.loc.aGrid);
    gl.vertexAttribPointer(this.loc.aGrid, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.ibo);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.heightTex);
    gl.uniform1i(this.loc.uHeight, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.rampTex);
    gl.uniform1i(this.loc.uRamp, 1);

    gl.uniformMatrix4fv(this.loc.uMVP, false, mvp);
    gl.uniform1f(this.loc.uOffset, this.head / this.depth);
    gl.uniform1f(this.loc.uAmp, this.amp);
    gl.uniform2f(this.loc.uTexel, 1 / this.depth, 1 / this.bins);

    gl.drawElements(gl.TRIANGLES, this.indexCount, gl.UNSIGNED_SHORT, 0);
  }

  clear() {
    if (!this.ok) return;
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.heightTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, this.depth, this.bins, 0,
                  gl.LUMINANCE, gl.UNSIGNED_BYTE, new Uint8Array(this.depth * this.bins));
    this.head = 0;
  }
}

window.Surface3D = Surface3D;

})();
