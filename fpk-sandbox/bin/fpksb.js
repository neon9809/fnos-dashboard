#!/usr/bin/env node
'use strict';
/**
 * fpksb — fnOS 应用沙箱配置 CLI
 *
 * A zero-dependency helper for authoring, validating and packing
 * 飞牛 fnOS application sandbox configurations into installable .fpk files.
 *
 * Docs: https://developer.fnnas.com/docs/quick-started
 */

const fs = require('node:fs');
const path = require('node:path');
const { checkProject, parseManifest } = require('../lib/check');
const { buildFpk, buildWithOfficialFnpack } = require('../lib/build');
const tpl = require('../lib/template');
const { renderLogo } = require('../lib/png');
const tar = require('../lib/tar');

const VERSION = '1.0.0';

// --------------------------------------------------------------------- utils

function die(msg) {
  console.error(`fpksb: ${msg}`);
  process.exit(1);
}

function parseArgs(argv) {
  const opts = {};
  const rest = [];
  const OPTS_WITH_VALUE = new Set(['o', 'C', 'c']);
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      const key = eq === -1 ? a.slice(2) : a.slice(2, eq);
      const val = eq === -1 ? (argv[i + 1] && !argv[i + 1].startsWith('--') && !argv[i + 1].startsWith('-') ? argv[++i] : true) : a.slice(eq + 1);
      opts[key] = val;
    } else if (/^-[a-zA-Z]$/.test(a)) {
      const key = a.slice(1);
      if (OPTS_WITH_VALUE.has(key)) {
        opts[key] = argv[++i];
      } else opts[key] = true;
    } else rest.push(a);
  }
  return { opts, rest };
}

function readManifest(dir) {
  const p = path.join(dir, 'manifest');
  if (!fs.existsSync(p)) die(`${dir} 下没有 manifest，请先 fpksb init 或确认目录`);
  return parseManifest(fs.readFileSync(p, 'utf8'));
}

function write(file, content) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

// ------------------------------------------------------------------ commands

async function cmdInit(opts, rest) {
  const appName = rest[0];
  if (!appName || !/^[a-z0-9][a-z0-9._-]*$/.test(appName))
    die('用法: fpksb init <appname> --image <docker-image> [options]');
  const image = opts.image;
  if (!image) die('必须通过 --image 指定 Docker 镜像（例如 --image nginx:alpine）');

  const outDir = path.resolve(String(opts.dir || appName));
  if (fs.existsSync(outDir) && fs.readdirSync(outDir).length > 0 && !opts.force)
    die(`目录 ${outDir} 非空（--force 覆盖）`);

  const o = {
    appName,
    image: String(image),
    displayName: String(opts['display-name'] || appName),
    desc: String(opts.desc || `${appName} fnOS 应用`),
    maintainer: String(opts.maintainer || 'developer'),
    maintainerUrl: opts['maintainer-url'] ? String(opts['maintainer-url']) : '',
    platform: ['x86', 'arm', 'all'].includes(String(opts.platform)) ? String(opts.platform) : 'all',
    servicePort: Number(opts.port || 8080),
    version: String(opts.version || '1.0.0'),
    net: opts.net === 'bridge' ? 'bridge' : 'host',
    containerName: String(opts['container-name'] || appName),
    dataMount: opts['data-mount'] ? String(opts['data-mount']) : '/app/data',
    dataShare: !!opts['data-share'],
  };

  write(path.join(outDir, 'manifest'), tpl.manifest(o));
  write(path.join(outDir, 'config/privilege'), tpl.privilege(o));
  write(path.join(outDir, 'config/resource'), tpl.resource(o));
  write(path.join(outDir, 'app/docker/docker-compose.yaml'), tpl.compose(o));
  // 随包携带的运行参数文件：安装后必然存在于 ${TRIM_APPDEST}/docker/env/app.env，
  // 保证 docker-project 拉起容器时 env_file 永不缺失
  write(path.join(outDir, 'app/docker/env/app.env'), tpl.shippedEnv(o));
  write(
    path.join(outDir, 'app/ui/config'),
    tpl.uiConfig(o)
  );
  write(path.join(outDir, 'cmd/main'), tpl.CMD_MAIN(o.containerName));
  for (const [f, c] of [
    ['install_init', '安装前执行'],
    ['install_callback', '安装完成后执行'],
    ['upgrade_init', '升级前执行'],
    ['upgrade_callback', '升级完成后执行'],
    ['uninstall_init', '卸载前执行'],
    ['uninstall_callback', '卸载清理后执行'],
    ['config_init', '应用配置变更前执行'],
    ['config_callback', '应用配置变更后执行'],
  ])
    write(path.join(outDir, `cmd/${f}`), tpl.CMD_STUB(c));
  fs.mkdirSync(path.join(outDir, 'wizard'), { recursive: true });
  fs.mkdirSync(path.join(outDir, 'app/ui/images'), { recursive: true });

  // icons
  fs.writeFileSync(path.join(outDir, 'ICON.PNG'), renderLogo(64));
  fs.writeFileSync(path.join(outDir, 'ICON_256.PNG'), renderLogo(256));
  fs.writeFileSync(path.join(outDir, 'app/ui/images/icon_64.png'), renderLogo(64));
  fs.writeFileSync(path.join(outDir, 'app/ui/images/icon_256.png'), renderLogo(256));

  // exec bits
  for (const f of fs.readdirSync(path.join(outDir, 'cmd'))) fs.chmodSync(path.join(outDir, 'cmd', f), 0o755);

  console.log(`✓ 已创建沙箱配置项目: ${outDir}`);
  console.log('  下一步:');
  console.log(`    cd ${path.relative(process.cwd(), outDir) || '.'}`);
    console.log('    # 编辑 app/docker/docker-compose.yaml 与 wizard/ 向导');
  console.log('    fpksb check   # 校验沙箱配置');
  console.log('    fpksb build   # 打包 .fpk');
}

async function cmdCheck(opts, rest) {
  const dir = path.resolve(rest[0] || '.');
  const result = await checkProject(dir);
  if (opts.json) {
    console.log(JSON.stringify(result, null, 2));
  } else {
    for (const n of result.notes) console.log(`\x1b[2mnote    ${n}\x1b[0m`);
    for (const w of result.warnings) console.log(`\x1b[33mwarning ${w}\x1b[0m`);
    for (const e of result.errors) console.log(`\x1b[31merror   ${e}\x1b[0m`);
    const ok = result.errors.length === 0;
    console.log(
      `\n${ok ? '\x1b[32m✓ 沙箱配置校验通过' : '\x1b[31m✗ 校验失败'}\x1b[0m ` +
        `(errors=${result.errors.length}, warnings=${result.warnings.length}) — ${dir}`
    );
  }
  process.exitCode = result.errors.length > 0 ? 1 : 0;
}

async function cmdBuild(opts, rest) {
  const dir = path.resolve(rest[0] || '.');
  const mf = readManifest(dir);
  if (!opts['no-check']) {
    const result = await checkProject(dir);
    if (result.errors.length > 0) {
      for (const e of result.errors) console.error(`\x1b[31merror   ${e}\x1b[0m`);
      die('校验未通过，已停止打包（--no-check 跳过校验）');
    }
    for (const w of result.warnings) console.log(`\x1b[33mwarning ${w}\x1b[0m`);
  }
  const packer = String(opts.packer || 'auto');
  let res;
  if (packer === 'internal') {
    const outFile = path.resolve(String(opts.out || opts.o || `${mf.appname}.fpk`));
    res = buildFpk(dir, outFile);
    console.log(`Packing successfully. The output file ${path.basename(res.outFile)} can be found in ${path.dirname(res.outFile)}.`);
  } else if (packer === 'fnpack' || packer === 'auto') {
    try {
      res = buildWithOfficialFnpack(dir, packer === 'fnpack');
      console.log(`✓ 由官方 fnpack 打包: ${res.outFile}`);
    } catch (e) {
      if (packer === 'fnpack') die(e.message);
      const outFile = path.resolve(String(opts.out || opts.o || `${mf.appname}.fpk`));
      console.log('\x1b[2m（未找到官方 fnpack，使用内置打包器）\x1b[0m');
      res = buildFpk(dir, outFile);
      console.log(`Packing successfully. The output file ${path.basename(res.outFile)} can be found in ${path.dirname(res.outFile)}.`);
    }
  } else die(`未知 packer: ${packer}（auto|internal|fnpack）`);
}

async function cmdUnpack(opts, rest) {
  const file = rest[0];
  if (!file) die('用法: fpksb unpack <file.fpk> [--list] [--deep] [-C outdir]');
  const buf = tar.gunzip(fs.readFileSync(file));
  const entries = tar.readTar(buf);
  const list = [];
  for (const e of entries) {
    list.push(e);
    if (e.path === 'app.tgz' && opts.deep) {
      const inner = tar.readTar(tar.gunzip(e.data));
      for (const ie of inner) list.push({ ...ie, path: `app/${ie.path}`, nested: true });
    }
  }
  if (opts.list) {
    for (const e of list) console.log(`${e.type === 'dir' ? 'd' : '-'} ${(e.mode & 0o777).toString(8).padStart(4, '0')} ${String(e.size).padStart(9)}  ${e.path}${e.nested ? '' : ''}`);
    return;
  }
  const out = path.resolve(String(opts.C || opts.c || path.basename(file, '.fpk') + '_extracted'));
  for (const e of list) {
    if (e.nested) continue; // app.tgz extracted as-is; use --deep listing to inspect
    const abs = path.join(out, e.path);
    if (e.type === 'dir') fs.mkdirSync(abs, { recursive: true });
    else {
      fs.mkdirSync(path.dirname(abs), { recursive: true });
      fs.writeFileSync(abs, e.data);
      fs.chmodSync(abs, e.mode || 0o644);
    }
  }
  console.log(`✓ 已解包到 ${out}（含 app.tgz；--deep 查看内部清单）`);
}

async function cmdIcons(opts, rest) {
  const outDir = path.resolve(String(rest[0] || '.'));
  const { iconsFromSource } = require('../lib/png');
  if (opts.from) {
    // 从源图生成全套图标：面积法缩放到 64/256，并按 fnOS 规范加圆角蒙版
    const srcPath = path.resolve(String(opts.from));
    if (!fs.existsSync(srcPath)) die(`源图不存在: ${srcPath}`);
    let res;
    try {
      res = iconsFromSource(fs.readFileSync(srcPath), {
        radius: Number(opts.radius || 0.2),
        round: !opts['no-round'],
      });
    } catch (e) {
      die(`解析 ${srcPath} 失败: ${e.message}`);
    }
    fs.writeFileSync(path.join(outDir, 'ICON.PNG'), res.icon64);
    fs.writeFileSync(path.join(outDir, 'ICON_256.PNG'), res.icon256);
    const ui = path.join(outDir, 'app/ui/images');
    fs.mkdirSync(ui, { recursive: true });
    fs.writeFileSync(path.join(ui, 'icon_64.png'), res.icon64);
    fs.writeFileSync(path.join(ui, 'icon_256.png'), res.icon256);
    console.log(`✓ 已从 ${path.basename(srcPath)} 生成 4 个圆角图标到 ${outDir}（--radius 调整圆角比例，--no-round 关闭圆角）`);
    return;
  }
  const sizes = { 'ICON.PNG': 64, 'ICON_256.PNG': 256 };
  for (const [f, s] of Object.entries(sizes)) fs.writeFileSync(path.join(outDir, f), renderLogo(s));
  const ui = path.join(outDir, 'app/ui/images');
  fs.mkdirSync(ui, { recursive: true });
  fs.writeFileSync(path.join(ui, 'icon_64.png'), renderLogo(64));
  fs.writeFileSync(path.join(ui, 'icon_256.png'), renderLogo(256));
  console.log(`✓ 图标已生成到 ${outDir}`);
}

const TRIM_VARS = [
  ['TRIM_APPNAME', 'manifest.appname 应用名称'],
  ['TRIM_APPVER', '当前应用版本'],
  ['TRIM_OLD_APPVER', '升级过程中的旧版本'],
  ['TRIM_APP_STATUS', '当前操作 INSTALL/START/UPGRADE/UNINSTALL/STOP/CONFIG'],
  ['TRIM_APPDEST', 'target 目录（应用文件）'],
  ['TRIM_PKGETC', '应用配置目录（etc）'],
  ['TRIM_PKGVAR', '运行时数据目录（var，重启保留）'],
  ['TRIM_PKGTMP', '临时目录（tmp）'],
  ['TRIM_PKGHOME', '应用用户数据目录（home）'],
  ['TRIM_PKGMETA', '元数据目录'],
  ['TRIM_APPDEST_VOL', '应用所在存储空间路径'],
  ['TRIM_USERNAME / TRIM_GROUPNAME', '专用应用用户 / 用户组（沙箱身份）'],
  ['TRIM_UID / TRIM_GID', '应用用户 UID / GID'],
  ['TRIM_RUN_USERNAME', '当前脚本执行用户（特权流程中可能不同）'],
  ['TRIM_SERVICE_PORT', 'manifest.service_port 服务端口'],
  ['TRIM_DATA_SHARE_PATHS', 'config/resource 数据共享路径（: 分隔）'],
  ['TRIM_DATA_ACCESSIBLE_PATHS', '用户授权的可访问路径（: 分隔）'],
  ['TRIM_API_TOKEN', '开放 API 认证 token'],
  ['TRIM_TEMP_LOGFILE', '用户可见错误输出文件（失败退出前写入）'],
  ['TRIM_SYS_VERSION(_MAJOR/_MINOR/_BUILD)', '系统版本'],
  ['TRIM_SYS_ARCH / TRIM_KERNEL_VERSION', 'CPU 架构 / 内核版本'],
  ['wizard_*', '向导收集的自定义字段（勿用 TRIM_ 前缀）'],
];

function cmdVars() {
  console.log('飞牛 fnOS 沙箱环境变量（生命周期脚本与 compose 可直接引用）：\n');
  for (const [k, v] of TRIM_VARS) console.log(`  \x1b[36m${k.padEnd(44)}\x1b[0m ${v}`);
  console.log('\ncompose 常用: ${TRIM_SERVICE_PORT} ${TRIM_APPDEST} ${TRIM_PKGVAR} ${TRIM_DATA_SHARE_PATHS}');
}

function usage() {
  console.log(`fpksb v${VERSION} — 飞牛 fnOS 应用沙箱配置 CLI

用法:
  fpksb init <appname> --image <img> [--port 8080] [--net host|bridge]
                     [--display-name ..] [--desc ..] [--maintainer ..]
                     [--platform all|x86|arm] [--container-name ..]
                     [--data-mount /app/data] [--data-share] [--dir out] [--force]
      生成完整沙箱配置项目骨架（manifest/privilege/resource/compose/cmd/wizard/icons）

  fpksb check [dir] [--json]
      校验沙箱配置（fnpack 清单 + 权限/资源/入口/端口一致性 + 图标/向导规范）

  fpksb build [dir] [-o file.fpk] [--packer auto|internal|fnpack] [--no-check]
      打包为 .fpk（auto：优先使用官方 fnpack，缺失时回退内置打包器）

  fpksb unpack <file.fpk> [--list] [--deep] [-C outdir]
      查看/解包 .fpk（含内部 app.tgz 清单）

  fpksb icons [dir]
      生成默认圆角图标 ICON.PNG/ICON_256.PNG 与入口图标

  fpksb vars
      打印沙箱环境变量（TRIM_*）参考表

文档: https://developer.fnnas.com/docs/quick-started`);
}

async function main() {
  const [, , cmd, ...restArgs] = process.argv;
  const { opts, rest } = parseArgs(restArgs || []);
  switch (cmd) {
    case 'init':
      return cmdInit(opts, rest);
    case 'check':
    case 'doctor':
      return cmdCheck(opts, rest);
    case 'build':
      return cmdBuild(opts, rest);
    case 'unpack':
      return cmdUnpack(opts, rest);
    case 'icons':
      return cmdIcons(opts, rest);
    case 'vars':
      return cmdVars();
    case '--version':
    case '-v':
      return console.log(`fpksb ${VERSION}`);
    default:
      usage();
      if (cmd) process.exitCode = 1;
  }
}

main().catch((e) => die(e.stack || e.message));
