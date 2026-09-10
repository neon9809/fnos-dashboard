'use strict';
/**
 * Zero-dependency PNG writer + procedural app-icon generator.
 *
 * Icons follow the fnOS icon guidelines: full square canvas, rounded-rect
 * visual body, sRGB, well below the 1024 KB size limit.
 */

const zlib = require('node:zlib');

// ---------------------------------------------------------------- PNG encode

const CRC_TABLE = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

/** pixels: RGBA buffer of width*height*4 bytes. */
function encodePNG(width, height, pixels) {
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // color type RGBA
  const stride = width * 4;
  const raw = Buffer.alloc((stride + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (stride + 1)] = 0; // filter: none
    // works for both Buffer and Uint8Array sources
    raw.set(pixels.subarray(y * stride, (y + 1) * stride), y * (stride + 1) + 1);
  }
  const idat = zlib.deflateSync(raw, { level: 9 });
  return Buffer.concat([sig, chunk('IHDR', ihdr), chunk('IDAT', idat), chunk('IEND', Buffer.alloc(0))]);
}

// --------------------------------------------------------------- icon render

function lerp(a, b, t) {
  return a + (b - a) * t;
}

function smoothstep(edge0, edge1, x) {
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

function sdRoundRect(px, py, cx, cy, hx, hy, r) {
  const qx = Math.abs(px - cx) - (hx - r);
  const qy = Math.abs(py - cy) - (hy - r);
  const ox = Math.max(qx, 0);
  const oy = Math.max(qy, 0);
  return Math.hypot(ox, oy) + Math.min(Math.max(qx, qy), 0) - r;
}

/** Signed distance to a cloud glyph made of circle unions (normalized coords). */
function sdCloud(px, py) {
  // puffs: big center, left small, right medium, plus base pill
  const circles = [
    [0.5, 0.44, 0.135],
    [0.355, 0.49, 0.093],
    [0.645, 0.475, 0.105],
  ];
  let d = Infinity;
  for (const [cx, cy, r] of circles) d = Math.min(d, Math.hypot(px - cx, py - cy) - r);
  // base rounded bar of the cloud
  d = Math.min(d, sdRoundRect(px, py, 0.5, 0.53, 0.185, 0.062, 0.06));
  return d;
}

/** Down-arrow inside the cloud (data sync motif). */
function sdArrow(px, py) {
  // vertical stem
  const stem = sdRoundRect(px, py, 0.5, 0.452, 0.016, 0.052, 0.014);
  // chevron head: two rotated bars -> approximate with triangle SDF
  const tx = px - 0.5;
  const ty = py - 0.512;
  // triangle with apex down at (0.5, 0.55), top edge y=0.48 half-width .075
  const apexDy = 0.55 - py;
  const halfW = 0.075 * (Math.max(0, apexDy) / 0.07); // widens towards bottom? invert:
  void halfW;
  const topY = 0.478;
  const botY = 0.552;
  const w = 0.08;
  let tri;
  if (py < topY || py > botY) {
    tri = Math.max(py < topY ? topY - py : py - botY, Math.abs(tx) - 0.001);
    if (py >= topY && py <= botY) tri = Math.abs(tx);
  } else {
    const t = (botY - py) / (botY - topY); // 1 near top, 0 near bottom
    tri = Math.abs(tx) - w * t;
  }
  const head = Math.max(tri, py > botY ? py - botY : topY - py);
  return Math.min(stem, head);
}

/**
 * Render the app logo at the requested size.
 * Returns a PNG buffer.
 */
function renderLogo(size) {
  const SS = 4; // supersampling factor
  const N = size * SS;
  const canvas = new Float64Array(N * N * 4);

  for (let y = 0; y < N; y++) {
    for (let x = 0; x < N; x++) {
      const nx = (x + 0.5) / N;
      const ny = (y + 0.5) / N;

      // rounded-square body occupying ~90% of the canvas
      const dBody = sdRoundRect(nx, ny, 0.5, 0.5, 0.452, 0.452, 0.108);
      const aa = SS / size * 1.25;
      const bodyA = smoothstep(aa, -aa, dBody);
      if (bodyA <= 0) continue;

      // diagonal gradient background (#3B82F6 -> #22D3EE)
      const t = Math.min(1, Math.max(0, (nx + ny) / 2));
      const r = lerp(59, 34, t) / 255;
      const g = lerp(130, 211, t) / 255;
      const b = lerp(246, 238, t) / 255;

      // subtle inner highlight ring
      const ring = smoothstep(-0.02, -0.005, Math.abs(dBody + 0.03)) * 0.12;

      // cloud glyph in white
      const dCloud = sdCloud(nx, ny);
      const cloudA = smoothstep(aa, -aa, dCloud);

      // arrow punched out of the cloud in gradient color
      const dArrow = sdArrow(nx, ny);
      const arrowA = smoothstep(aa, -aa, dArrow);

      const alpha = bodyA;
      let cr = lerp(r, 1, cloudA);
      let cg = lerp(g, 1, cloudA);
      let cb = lerp(b, 1, cloudA);
      cr = lerp(cr, r * 0.9 + 0.1, arrowA);
      cg = lerp(cg, g, arrowA);
      cb = lerp(cb, b, arrowA);
      const shade = 1 - ring;

      const i = (y * N + x) * 4;
      canvas[i] = cr * shade;
      canvas[i + 1] = cg * shade;
      canvas[i + 2] = cb * shade;
      canvas[i + 3] = alpha;
    }
  }

  // box downsample
  const out = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let r = 0, g = 0, b = 0, a = 0;
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const i = ((y * SS + sy) * N + (x * SS + sx)) * 4;
          const wa = canvas[i + 3];
          r += canvas[i] * wa;
          g += canvas[i + 1] * wa;
          b += canvas[i + 2] * wa;
          a += wa;
        }
      }
      const o = (y * size + x) * 4;
      if (a > 0) {
        out[o] = Math.round((r / a) * 255);
        out[o + 1] = Math.round((g / a) * 255);
        out[o + 2] = Math.round((b / a) * 255);
      }
      out[o + 3] = Math.round((a / (SS * SS)) * 255);
    }
  }
  return encodePNG(size, size, out);
}

module.exports = { encodePNG, renderLogo, decodePNG, resizeArea, applyRoundedMask, iconsFromSource };

// ---------------------------------------------------------------- PNG decode

/**
 * Decode an 8-bit non-interlaced PNG (color types 0/2/6) into RGBA.
 * Enough for app-icon sources; not a general-purpose image codec.
 */
function decodePNG(buf) {
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  if (!buf.subarray(0, 8).equals(sig)) throw new Error('不是合法 PNG（签名不符）');
  let off = 8;
  let width = 0, height = 0, bitDepth = 0, colorType = 0, interlace = 0;
  const idatParts = [];
  while (off + 8 <= buf.length) {
    const len = buf.readUInt32BE(off);
    const type = buf.toString('ascii', off + 4, off + 8);
    const data = buf.subarray(off + 8, off + 8 + len);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
      interlace = data[12];
    } else if (type === 'IDAT') {
      idatParts.push(data);
    } else if (type === 'IEND') {
      break;
    }
    off += 12 + len;
  }
  if (!width || !height) throw new Error('PNG 缺少 IHDR');
  if (bitDepth !== 8) throw new Error(`暂不支持位深 ${bitDepth}（需要 8-bit）`);
  if (interlace !== 0) throw new Error('暂不支持隔行扫描的 PNG');
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : colorType === 0 ? 1 : null;
  if (!channels) throw new Error(`暂不支持颜色类型 ${colorType}（支持 RGB/RGBA/灰度）`);

  const raw = zlib.inflateSync(Buffer.concat(idatParts));
  const stride = width * channels;
  const expected = (stride + 1) * height;
  if (raw.length < expected) throw new Error('PNG 像素数据不完整');

  // undo per-scanline filters
  const pixels = new Uint8Array(width * height * 4);
  const line = Buffer.alloc(stride);
  const prev = Buffer.alloc(stride);
  let rp = 0;
  const paeth = (a, b, c) => {
    const p = a + b - c;
    const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
    return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
  };
  for (let y = 0; y < height; y++) {
    const filter = raw[rp++];
    raw.copy(line, 0, rp, rp + stride);
    rp += stride;
    for (let x = 0; x < stride; x++) {
      const left = x >= channels ? line[x - channels] : 0;
      const up = y > 0 ? prev[x] : 0;
      const ul = y > 0 && x >= channels ? prev[x - channels] : 0;
      switch (filter) {
        case 0: break;
        case 1: line[x] = (line[x] + left) & 0xff; break;
        case 2: line[x] = (line[x] + up) & 0xff; break;
        case 3: line[x] = (line[x] + ((left + up) >> 1)) & 0xff; break;
        case 4: line[x] = (line[x] + paeth(left, up, ul)) & 0xff; break;
        default: throw new Error(`未知 PNG 行滤波类型 ${filter}`);
      }
    }
    // expand to RGBA
    for (let x = 0; x < width; x++) {
      const o = (y * width + x) * 4;
      if (channels === 1) {
        pixels[o] = pixels[o + 1] = pixels[o + 2] = line[x];
        pixels[o + 3] = 255;
      } else {
        pixels[o] = line[x * channels];
        pixels[o + 1] = line[x * channels + 1];
        pixels[o + 2] = line[x * channels + 2];
        pixels[o + 3] = channels === 4 ? line[x * channels + 3] : 255;
      }
    }
    line.copy(prev);
  }
  return { width, height, data: pixels };
}

/** Area-average resample (high quality for downscaling). */
function resizeArea(src, dw, dh) {
  const out = new Uint8Array(dw * dh * 4);
  const { width: sw, height: sh, data } = src;
  for (let dy = 0; dy < dh; dy++) {
    const fy0 = (dy * sh) / dh;
    const fy1 = ((dy + 1) * sh) / dh;
    for (let dx = 0; dx < dw; dx++) {
      const fx0 = (dx * sw) / dw;
      const fx1 = ((dx + 1) * sw) / dw;
      let r = 0, g = 0, b = 0, a = 0, wsum = 0;
      for (let sy = Math.floor(fy0); sy < Math.min(sh, Math.ceil(fy1)); sy++) {
        const wy = Math.min(sy + 1, fy1) - Math.max(sy, fy0);
        if (wy <= 0) continue;
        for (let sx = Math.floor(fx0); sx < Math.min(sw, Math.ceil(fx1)); sx++) {
          const wx = Math.min(sx + 1, fx1) - Math.max(sx, fx0);
          if (wx <= 0) continue;
          const wgt = wx * wy;
          const i = (sy * sw + sx) * 4;
          r += data[i] * wgt;
          g += data[i + 1] * wgt;
          b += data[i + 2] * wgt;
          a += data[i + 3] * wgt;
          wsum += wgt;
        }
      }
      const o = (dy * dw + dx) * 4;
      if (wsum > 0) {
        out[o] = Math.round(r / wsum);
        out[o + 1] = Math.round(g / wsum);
        out[o + 2] = Math.round(b / wsum);
        out[o + 3] = Math.round(a / wsum);
      }
    }
  }
  return out;
}

/**
 * Apply an anti-aliased rounded-rectangle mask in place (fnOS icon style:
 * full-square canvas with rounded visual body instead of hard square edges).
 * radiusRatio: corner radius as a fraction of the canvas side.
 */
function applyRoundedMask(pixels, size, radiusRatio = 0.2, ss = 4) {
  const N = size * ss;
  const cov = new Float64Array(N * N);
  const aa = (ss / size) * 1.25;
  for (let y = 0; y < N; y++) {
    for (let x = 0; x < N; x++) {
      const nx = (x + 0.5) / N;
      const ny = (y + 0.5) / N;
      const d = sdRoundRectFull(nx, ny, 0.5, 0.5, 0.5, 0.5, radiusRatio);
      cov[y * N + x] = smoothstep(aa, -aa, d);
    }
  }
  for (let py = 0; py < size; py++) {
    for (let px = 0; px < size; px++) {
      let c = 0;
      for (let sy = 0; sy < ss; sy++)
        for (let sx = 0; sx < ss; sx++) c += cov[(py * ss + sy) * N + (px * ss + sx)];
      c /= ss * ss;
      const o = (py * size + px) * 4;
      pixels[o + 3] = Math.round(pixels[o + 3] * c);
    }
  }
  return pixels;
}

// sdRoundRect exists above for the logo renderer; reuse it.
function sdRoundRectFull(px, py, cx, cy, hx, hy, r) {
  const qx = Math.abs(px - cx) - (hx - r);
  const qy = Math.abs(py - cy) - (hy - r);
  const ox = Math.max(qx, 0);
  const oy = Math.max(qy, 0);
  return Math.hypot(ox, oy) + Math.min(Math.max(qx, qy), 0) - r;
}

/** Build 64/256 icon PNG buffers from a source PNG buffer. */
function iconsFromSource(sourcePngBuffer, { radius = 0.2, round = true } = {}) {
  const src = decodePNG(sourcePngBuffer);
  const make = (size) => {
    let img = resizeArea(src, size, size);
    if (round) img = applyRoundedMask(img, size, radius);
    return encodePNG(size, size, img);
  };
  return { icon64: make(64), icon256: make(256) };
}
