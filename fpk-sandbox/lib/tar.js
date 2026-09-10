'use strict';
/**
 * Minimal ustar tar reader/writer + gzip helpers for the .fpk format.
 *
 * .fpk layout (reverse-engineered from official fnpack 1.2.3 output):
 *   <name>.fpk            = gzip( tar(
 *     app.tgz               = gzip( tar( app/**  +  config/** ) )   <- config is duplicated inside
 *     cmd/...               lifecycle scripts
 *     config/privilege      sandbox run-as configuration
 *     config/resource       sandbox resource declarations
 *     ICON.PNG / ICON_256.PNG
 *     manifest
 *     wizard/
 *   ))
 */

const zlib = require('node:zlib');

const BLOCK = 512;

function octal(value, length) {
  // length includes the trailing NUL byte
  return value.toString(8).padStart(length - 1, '0') + '\0';
}

function stringField(value, length) {
  const buf = Buffer.alloc(length, 0);
  if (value) Buffer.from(value, 'utf8').copy(buf, 0, 0, Math.min(length - 1, Buffer.byteLength(value)));
  return buf;
}

/** Split a long path into (prefix, name) per ustar rules. */
function splitName(name) {
  if (Buffer.byteLength(name, 'utf8') <= 100) return { prefix: '', name };
  let idx = name.length;
  while (idx > 0) {
    const cut = name.lastIndexOf('/', idx - 1);
    if (cut <= 0) break;
    const head = name.slice(0, cut);
    const tail = name.slice(cut + 1);
    if (Buffer.byteLength(head, 'utf8') <= 155 && Buffer.byteLength(tail, 'utf8') <= 100) {
      return { prefix: head, name: tail };
    }
    idx = cut;
  }
  throw new Error(`tar: path too long: ${name}`);
}

function tarHeader(entry) {
  const { prefix, name } = splitName(entry.path);
  const buf = Buffer.alloc(BLOCK, 0);
  stringField(name, 100).copy(buf, 0);
  buf.write(octal(entry.mode != null ? entry.mode : 0o644, 8), 100);
  buf.write(octal(entry.uid != null ? entry.uid : 0, 8), 108);
  buf.write(octal(entry.gid != null ? entry.gid : 0, 8), 116);
  buf.write(octal(entry.size || 0, 12), 124);
  buf.write(octal(Math.floor((entry.mtime || 0) / 1000), 12), 136);
  // checksum placeholder (spaces)
  buf.write('        ', 148);
  buf.write(entry.type === 'dir' ? '5' : entry.type === 'link' ? '2' : '0', 156);
  stringField(entry.linkname || '', 100).copy(buf, 157);
  buf.write('ustar\0', 257, 'binary');
  buf.write('00', 263, 'binary');
  stringField(entry.uname || '', 32).copy(buf, 265);
  stringField(entry.gname || '', 32).copy(buf, 297);
  buf.write('0000000\0', 329); // devmajor
  buf.write('0000000\0', 337); // devminor
  stringField(prefix, 155).copy(buf, 345);
  let sum = 0;
  for (const b of buf) sum += b;
  buf.write(sum.toString(8).padStart(6, '0') + '\0 ', 148);
  return buf;
}

/**
 * Serialize entries into a tar buffer.
 * entries: [{ path, type: 'file'|'dir', mode, size, mtime, uid, gid, uname, gname, data(Buffer) }]
 */
function writeTar(entries) {
  const chunks = [];
  for (const e of entries) {
    chunks.push(tarHeader(e));
    if (e.type !== 'dir' && e.size > 0) {
      chunks.push(e.data);
      const pad = (BLOCK - (e.data.length % BLOCK)) % BLOCK;
      if (pad) chunks.push(Buffer.alloc(pad, 0));
    }
  }
  chunks.push(Buffer.alloc(BLOCK * 2, 0)); // end-of-archive
  return Buffer.concat(chunks);
}

/** Parse a tar buffer into entries. */
function readTar(buf) {
  const out = [];
  let off = 0;
  while (off + BLOCK <= buf.length) {
    const header = buf.subarray(off, off + BLOCK);
    if (header.every((b) => b === 0)) break;
    const size = parseInt(header.toString('utf8', 124, 136).replace(/\0.*$/, ''), 8) || 0;
    const prefix = header.toString('utf8', 345, 500).replace(/\0.*$/, '');
    let name = header.toString('utf8', 0, 100).replace(/\0.*$/, '');
    if (prefix) name = `${prefix}/${name}`;
    const typeByte = header.toString('utf8', 156, 157);
    off += BLOCK;
    const data = size > 0 ? Buffer.from(buf.subarray(off, off + size)) : Buffer.alloc(0);
    off += size + ((BLOCK - (size % BLOCK)) % BLOCK);
    out.push({
      path: name,
      type: typeByte === '5' ? 'dir' : typeByte === '2' ? 'link' : 'file',
      mode: parseInt(header.toString('utf8', 100, 108).replace(/[^0-7]/g, ''), 8) || 0,
      size,
      data,
    });
  }
  return out;
}

function gzip(data) {
  return zlib.gzipSync(data, { level: 9 });
}

function gunzip(data) {
  return zlib.gunzipSync(data);
}

module.exports = { writeTar, readTar, gzip, gunzip, BLOCK };
