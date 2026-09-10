'use strict';
/**
 * .fpk packer (`fpksb build`).
 *
 * Replicates the official fnpack 1.2.3 output format:
 *   outer tar.gz = [ app.tgz, cmd/**, config/**, ICON.PNG, ICON_256.PNG, manifest, wizard/ ]
 *   app.tgz      = gzip( tar( app/** ++ config/** ) )   <- config/ is duplicated inside,
 *                   matching the reference output of `fnpack build`.
 *
 * If the official `fnpack` binary is available on PATH, `--packer auto|fnpack`
 * delegates to it; `internal` always uses this implementation.
 */

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const tar = require('./tar');

const EXCLUDE = new Set(['.DS_Store', 'Thumbs.db', '.git', '__MACOSX']);
const EXCLUDE_SUFFIX = ['.fpk'];

function userInfo() {
  try {
    const u = os.userInfo();
    return { uid: u.uid, gid: u.gid, uname: u.username, gname: u.username };
  } catch {
    return { uid: 0, gid: 0, uname: '', gname: '' };
  }
}

function shouldExclude(name) {
  if (EXCLUDE.has(name)) return true;
  if (name.startsWith('._')) return true;
  for (const s of EXCLUDE_SUFFIX) if (name.endsWith(s)) return true;
  return false;
}

/** Walk a directory, producing sorted entries (case-insensitive, dirs before their content). */
function walk(base, rel = '') {
  const absDir = path.join(base, rel);
  const names = fs.readdirSync(absDir).filter((n) => !shouldExclude(n));
  names.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const out = [];
  for (const name of names) {
    const abs = path.join(absDir, name);
    const r = rel ? `${rel}/${name}` : name;
    const st = fs.statSync(abs);
    if (st.isDirectory()) {
      out.push({ rel: r + '/', abs, dir: true });
      out.push(...walk(base, r));
    } else {
      out.push({ rel: r, abs, dir: false });
    }
  }
  return out;
}

function entryFor(file, base, user) {
  const st = fs.statSync(file.abs);
  let mode;
  if (file.dir) {
    mode = 0o755;
  } else {
    // normalize like the reference output: 0644 for data files,
    // 0755 when the source is executable or it is a lifecycle script
    const isExecSource = (st.mode & 0o111) !== 0;
    const isScript =
      (file.rel.startsWith('cmd/') && !file.rel.includes('/.')) ||
      file.rel.endsWith('.sh') ||
      file.rel.endsWith('.cgi');
    mode = isExecSource || isScript ? 0o755 : 0o644;
  }
  return {
    path: file.rel.replace(/\/$/, ''),
    type: file.dir ? 'dir' : 'file',
    mode,
    size: file.dir ? 0 : st.size,
    mtime: st.mtimeMs,
    uid: user.uid,
    gid: user.gid,
    uname: user.uname,
    gname: user.gname,
    data: file.dir ? Buffer.alloc(0) : fs.readFileSync(file.abs),
  };
}

function buildFpk(dir, outFile) {
  const user = userInfo();
  const rootNames = ['cmd', 'config', 'wizard'];

  // Emit a directory entry for `rel` followed by its recursive children (prefixed).
  function emitTree(absBase, prefix) {
    const out = [
      entryFor({ rel: prefix + '/', abs: absBase, dir: true }, dir, user),
      ...walk(absBase).map((f) => entryFor({ ...f, rel: `${prefix}/${f.rel}`.replace(/\/+$/, f.dir ? '/' : '') }, dir, user)),
    ];
    return out;
  }

  // ---- inner app.tgz: contents of app/ then a duplicate of config/
  const appFiles = walk(path.join(dir, 'app'));
  const appEntries = [
    ...appFiles.map((f) => entryFor(f, path.join(dir, 'app'), user)),
    ...emitTree(path.join(dir, 'config'), 'config'),
  ];
  const appTgz = tar.gzip(tar.writeTar(appEntries));

  // ---- outer members
  const entries = [];
  entries.push({
    path: 'app.tgz',
    type: 'file',
    mode: 0o644,
    size: appTgz.length,
    mtime: Date.now(),
    ...user,
    data: appTgz,
  });

  for (const name of rootNames) {
    const abs = path.join(dir, name);
    if (!fs.existsSync(abs)) continue;
    entries.push(...emitTree(abs, name));
  }
  for (const name of ['ICON.PNG', 'ICON_256.PNG']) {
    const abs = path.join(dir, name);
    if (!fs.existsSync(abs)) continue;
    entries.push(entryFor({ rel: name, abs, dir: false }, dir, user));
  }

  // manifest: fnpack appends "checksum = md5(app.tgz)" during packing;
  // replicate it so installers can verify package integrity.
  {
    let mf = fs.readFileSync(path.join(dir, 'manifest'), 'utf8');
    mf = mf.replace(/^checksum\s*=.*$\n?/gm, '').replace(/\n*$/, '\n');
    const md5 = require('node:crypto').createHash('md5').update(appTgz).digest('hex');
    mf += `checksum              = ${md5}\n`;
    entries.push({
      path: 'manifest',
      type: 'file',
      mode: 0o644,
      size: Buffer.byteLength(mf),
      mtime: Date.now(),
      ...user,
      data: Buffer.from(mf),
    });
  }

  // Official output pins app.tgz first, then sorts members case-insensitively
  // (parent dir entry directly precedes its children).
  const head = entries.filter((e) => e.path === 'app.tgz');
  const rest = entries
    .filter((e) => e.path !== 'app.tgz')
    .sort((a, b) => {
      const x = a.path.toLowerCase();
      const y = b.path.toLowerCase();
      return x < y ? -1 : x > y ? 1 : 0;
    });
  const ordered = [...head, ...rest];

  const fpk = tar.gzip(tar.writeTar(ordered));
  fs.mkdirSync(path.dirname(path.resolve(outFile)), { recursive: true });
  fs.writeFileSync(outFile, fpk);
  return { outFile, bytes: fpk.length, files: appEntries.filter((e) => e.type === 'file').length + entries.length - 1 };
}

function findFnpack() {
  const candidates = [process.env.FNPACK_BIN, '/usr/local/bin/fnpack', '/usr/bin/fnpack'].filter(Boolean);
  for (const c of candidates) {
    try {
      fs.accessSync(c, fs.constants.X_OK);
      return c;
    } catch {
      /* keep looking */
    }
  }
  try {
    return execFileSync('which', ['fnpack'], { encoding: 'utf8' }).trim() || null;
  } catch {
    return null;
  }
}

function buildWithOfficialFnpack(dir, binPath) {
  const bin = binPath === true ? findFnpack() : binPath;
  if (!bin) throw new Error('未找到官方 fnpack（可设置 FNPACK_BIN 或使用 --packer internal）');
  execFileSync(bin, ['build', '--directory', path.resolve(dir)], { stdio: 'inherit' });
  const name = require('./check').parseManifest(fs.readFileSync(path.join(dir, 'manifest'), 'utf8')).appname;
  return { outFile: path.join(path.resolve(dir), `${name}.fpk`), via: 'fnpack' };
}

module.exports = { buildFpk, buildWithOfficialFnpack, findFnpack };
