'use strict';
/**
 * Sandbox-configuration validator (`fpksb check`).
 *
 * Validates a project against the fnOS packaging contract:
 *   1. fnpack build checklist (required files / dirs)
 *   2. manifest field rules
 *   3. config/privilege — least-privilege run-as model
 *   4. config/resource — docker-project / data-share declarations
 *   5. app/ui/config — desktop entry consistency
 *   6. docker-compose — container_name, port mapping vs manifest.service_port,
 *      network_mode conflicts
 *   7. cmd/* lifecycle scripts — status handling, exec bits
 *   8. wizard files — JSON schema-ish validation and TRIM_ prefix guard
 *   9. icons — PNG signature, dimensions (64/256), size limit
 */

const fs = require('node:fs');
const path = require('node:path');

function readText(p) {
  return fs.readFileSync(p, 'utf8');
}

function exists(p) {
  try {
    fs.statSync(p);
    return true;
  } catch {
    return false;
  }
}

function isDir(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

/** Parse the shell-like `key = value` manifest format. */
function parseManifest(text) {
  const out = {};
  for (let line of text.split(/\r?\n/)) {
    line = line.trim();
    if (!line || line.startsWith('#')) continue;
    const m = line.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (m) out[m[1]] = m[2].trim().replace(/^"|"$/g, '');
  }
  return out;
}

function pngSize(file) {
  const buf = fs.readFileSync(file);
  if (buf.length < 33) return null;
  if (!(buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47)) return 'not-png';
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20), bytes: buf.length };
}

/** Compare dotted numeric versions: returns true when a < b. */
function versionLt(a, b) {
  const pa = String(a).split('.').map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split('.').map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0;
    const y = pb[i] || 0;
    if (x < y) return true;
    if (x > y) return false;
  }
  return false;
}

const KNOWN_WIZARD_TYPES = new Set(['text', 'password', 'radio', 'checkbox', 'select', 'switch', 'tips']);

/**
 * @returns {Promise<{errors:string[], warnings:string[], notes:string[]}>}
 */
async function checkProject(dir, opts = {}) {
  const errors = [];
  const warnings = [];
  const notes = [];

  const E = (m) => errors.push(m);
  const W = (m) => warnings.push(m);
  const N = (m) => notes.push(m);

  // ---------------------------------------------------------------- layout
  const requiredFiles = ['manifest', 'config/privilege', 'config/resource', 'ICON.PNG', 'ICON_256.PNG'];
  for (const f of requiredFiles) {
    if (!exists(path.join(dir, f))) E(`缺少必需文件: ${f}`);
  }
  for (const d of ['app', 'cmd', 'wizard']) {
    if (!isDir(path.join(dir, d))) E(`缺少必需目录: ${d}/`);
  }
  if (errors.filter((e) => e.startsWith('缺少')).length > 0 && opts.strictLayout) {
    return { errors, warnings, notes };
  }

  // -------------------------------------------------------------- manifest
  let mf = {};
  if (exists(path.join(dir, 'manifest'))) {
    mf = parseManifest(readText(path.join(dir, 'manifest')));
    if (!mf.appname) E('manifest 缺少 appname（应用唯一标识）');
    else {
      if (!/^[a-z0-9][a-z0-9._-]*$/.test(mf.appname)) E(`manifest.appname 非法: "${mf.appname}"（建议小写字母/数字/-）`);
      if (Buffer.byteLength(mf.appname) > 32) W('manifest.appname 过长，将影响路径与用户名生成');
    }
    if (!mf.version) E('manifest 缺少 version');
    else if (!/^\d+\.\d+\.\d+(-[\w.]+)?$/.test(mf.version)) W(`manifest.version 不是语义化版本: ${mf.version}`);
    if (!mf.display_name) E('manifest 缺少 display_name');
    if (!mf.desc) W('manifest 建议填写 desc 应用描述');
    if (!mf.source) W('manifest.source 未设置，第三方应用应使用 thirdparty');
    else if (mf.source !== 'thirdparty') N(`manifest.source=${mf.source}`);
    if (!mf.platform) E('manifest 缺少 platform（x86|arm|all）');
    else if (!['x86', 'arm', 'all'].includes(mf.platform)) E(`manifest.platform 非法: ${mf.platform}`);
    if (!mf.maintainer) W('manifest 建议填写 maintainer');
    if (mf.service_port && !/^\d+$/.test(String(mf.service_port))) E('manifest.service_port 必须是数字');
    if (mf.desktop_uidir && /[\\/]|^\.+$/.test(mf.desktop_uidir)) E('manifest.desktop_uidir 应为相对目录名（如 ui）');

    // 入口动态变量（${wizard_*}）需要系统版本支持：文档标注入口环境变量 V1.1.8+
    const uiCfgPath = path.join(dir, 'app', mf.desktop_uidir || 'ui', 'config');
    if (!mf.os_min_version && exists(uiCfgPath) && /\$\{wizard_/.test(readText(uiCfgPath)))
      E('入口使用 ${wizard_*} 动态变量但未声明 os_min_version（该能力要求系统 ≥ 1.1.8）');
    if (mf.os_min_version && exists(uiCfgPath) && /\$\{wizard_/.test(readText(uiCfgPath))) {
      if (versionLt(mf.os_min_version, '1.1.8'))
        E(`os_min_version=${mf.os_min_version} 低于入口动态变量所要求的 1.1.8`);
      else N(`os_min_version=${mf.os_min_version}（满足入口动态变量要求的 1.1.8）`);
    }

    // Docker + platform=all sanity
    const composePath1 = path.join(dir, 'app/docker/docker-compose.yaml');
    const composePath2 = path.join(dir, 'app/docker/docker-compose.yml');
    const composePath = exists(composePath1) ? composePath1 : exists(composePath2) ? composePath2 : null;
    if (composePath && mf.platform === 'all') {
      N('platform=all：请确认镜像为多架构（amd64/arm64），否则容器无法在对应设备启动');
    }
  }

  // ------------------------------------------------------------- privilege
  let priv = {};
  if (exists(path.join(dir, 'config/privilege'))) {
    try {
      priv = JSON.parse(readText(path.join(dir, 'config/privilege')));
      const runAs = priv.defaults && priv.defaults['run-as'];
      if (!runAs) E('config/privilege 缺少 defaults.run-as');
      else if (runAs === 'root') W('run-as=root 会放大安全风险：仅生命周期脚本需要特权时使用，服务进程应降权到包用户');
      else if (runAs !== 'package') E(`config/privilege defaults.run-as 非法: ${runAs}（应为 package 或 root）`);
      if (runAs === 'package' && !priv.username) N('未显式配置 username/groupname，系统将按 appname 自动生成包用户');
    } catch (e) {
      E(`config/privilege 不是合法 JSON: ${e.message}`);
    }
  }

  // --------------------------------------------------------------- resource
  if (exists(path.join(dir, 'config/resource'))) {
    let res;
    try {
      res = JSON.parse(readText(path.join(dir, 'config/resource')));
    } catch (e) {
      E(`config/resource 不是合法 JSON: ${e.message}`);
      res = null;
    }
    if (res) {
      const projects = res['docker-project'] && res['docker-project'].projects;
      if (projects !== undefined) {
        if (!Array.isArray(projects) || projects.length === 0) E('docker-project.projects 必须是非空数组');
        else {
          projects.forEach((p, i) => {
            if (!p.name) E(`docker-project.projects[${i}] 缺少 name`);
            if (!p.path) E(`docker-project.projects[${i}] 缺少 path`);
            else {
              const abs = path.join(dir, 'app', p.path);
              if (!isDir(abs)) E(`docker-project 路径不存在: app/${p.path}`);
              else if (!exists(path.join(abs, 'docker-compose.yaml')) && !exists(path.join(abs, 'docker-compose.yml')))
                E(`app/${p.path} 下没有 docker-compose.yaml`);
            }
          });
        }
      }
      const shares = res['data-share'] && res['data-share'].shares;
      if (shares !== undefined) {
        for (const s of shares) {
          if (!s.name) E('data-share.shares[] 缺少 name');
          else if (mf.appname && !String(s.name).startsWith(`${mf.appname}`))
            W(`data-share "${s.name}" 未以应用名 "${mf.appname}" 为顶级目录，易与其他应用冲突`);
        }
      }
      if (res['usr-local-linker']) N('使用了 usr-local-linker：请确认暴露的命令名带应用标识，避免与系统命令冲突');
    }
  }

  // --------------------------------------------------------------- ui entry
  const uidirName = (mf && mf.desktop_uidir) || 'ui';
  const uiDir = path.join(dir, 'app', uidirName);
  if (!isDir(uiDir) && mf.desktop_applaunchname) E(`desktop_uidir 不存在: app/${uidirName}/`);
  if (exists(path.join(uiDir, 'config'))) {
    let ui;
    try {
      ui = JSON.parse(readText(path.join(uiDir, 'config')));
    } catch (e) {
      E(`app/${uidirName}/config 不是合法 JSON: ${e.message}`);
      ui = null;
    }
    if (ui) {
      const entries = ui['.url'];
      if (!entries || typeof entries !== 'object') E(`app/${uidirName}/config 缺少 .url 入口定义`);
      else {
        const ids = Object.keys(entries);
        for (const id of ids) {
          const en = entries[id];
          if (mf.appname && !id.startsWith(mf.appname)) W(`入口 ID "${id}" 建议以应用名 "${mf.appname}" 为前缀`);
          if (en.icon && String(en.icon).includes('{0}')) {
            for (const size of [64, 256]) {
              const rel = String(en.icon).replace('{0}', String(size));
              const f = path.join(uiDir, rel);
              if (!exists(f)) E(`入口图标缺失: app/${uidirName}/${rel}`);
            }
          }
          if (en.type && !['iframe', 'url'].includes(en.type)) W(`入口 ${id} type="${en.type}" 不是 iframe/url`);
        }
        if (mf.desktop_applaunchname && !ids.includes(mf.desktop_applaunchname))
          E(`manifest.desktop_applaunchname="${mf.desktop_applaunchname}" 未在 .url 中定义`);
        if (mf.service_port) {
          for (const id of ids) {
            const en = entries[id];
            if (en.port && /^\d+$/.test(String(en.port)) && String(en.port) !== String(mf.service_port))
              E(`入口 ${id} port=${en.port} 与 manifest.service_port=${mf.service_port} 不一致`);
          }
        }
      }
    }
  } else if (isDir(uiDir)) {
    W(`app/${uidirName}/config 不存在：应用将没有桌面入口（--without-ui 类应用可忽略）`);
  }

  // ---------------------------------------------------------------- compose
  const cPaths = ['app/docker/docker-compose.yaml', 'app/docker/docker-compose.yml'].map((p) => path.join(dir, p));
  const cPath = cPaths.find(exists);
  if (cPath) {
    const yaml = readText(cPath);

    // 按 service 拆分（两空格缩进的顶层 service 块），用于逐服务检查网络/端口组合
    const serviceBlocks = (() => {
      const out = {};
      let cur = null;
      for (const ln of yaml.split(/\r?\n/)) {
        if (/^services:\s*$/.test(ln)) continue;
        if (/^\s*#/.test(ln) || !ln.trim()) continue;
        const m = ln.match(/^ {2}([^\s#][^:]*):\s*(.*)$/);
        if (m && !/^ {3}/.test(ln)) {
          cur = m[1];
          out[cur] = [];
          continue;
        }
        if (cur && /^ {4,}/.test(ln)) out[cur].push(ln);
      }
      return out;
    })();

    const containerNames = [...yaml.matchAll(/^\s*container_name:\s*(\S+)/gm)].map((m) => m[1]);
    if (containerNames.length === 0) W('compose 未声明 container_name：cmd/main 的 status 检查需要按实际容器名调整');
    else if (new Set(containerNames).size !== containerNames.length)
      N('compose 中多个 service 复用同一 container_name：请确认它们属于互斥的 profiles，不会同时创建');

    let anyHost = false;
    for (const [name, body] of Object.entries(serviceBlocks)) {
      const b = body.join('\n');
      const hasHost = /^\s*network_mode:\s*host\s*$/m.test(b);
      const hasPorts = /^\s*ports:\s*$/m.test(b) && /^\s*-\s*\S+/.test(body.filter((l) => /^\s{6,}-/.test(l)).join('\n') || b);
      if (hasHost) anyHost = true;
      if (hasHost && hasPorts) E(`service "${name}" 同时存在 network_mode: host 与 ports 映射（host 模式不能映射端口）`);
    }

    // 镜像标签：latest 或缺省 tag 会导致同一 .fpk 不同时间装出不同内容，禁止
    const images = [...yaml.matchAll(/^\s*(?:-\s*)?image:\s*(\S+)/gm)].map((m) => m[1]);
    if (images.length === 0) W('compose 中未发现 image 字段（确认是否使用 build 构建而非预构建镜像）');
    for (const img of images) {
      let tag = '';
      if (img.includes('@sha256:')) tag = 'digest';
      else {
        const last = img.split('/').pop();
        tag = last.includes(':') ? last.slice(last.indexOf(':') + 1) : '';
      }
      if (!tag) E(`镜像 ${img} 缺少版本标签：同一 .fpk 在不同时间安装会得到不同内容，请固定具体版本`);
      else if (/^(latest|stable|main|master)$/i.test(tag))
        E(`镜像 ${img} 使用了可变标签 "${tag}"：请固定为具体版本号（与 manifest.version 一致），便于审核复现与回滚`);
      else N(`镜像已固定版本: ${img}`);
    }

    // compose 插值变量：TRIM_* 之外的变量必须由随包 .env 提供
    const dotenvPath = path.join(path.dirname(cPath), '.env');
    const usedVars = [...yaml.matchAll(/\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}/g)].map((m) => m[1]);
    const trimVars = usedVars.filter((v) => v.startsWith('TRIM_') || v.startsWith('wizard_'));
    const appVars = [...new Set(usedVars.filter((v) => !v.startsWith('TRIM_') && !v.startsWith('wizard_')))];
    if (/profiles:/m.test(yaml)) {
      if (!exists(dotenvPath))
        E('compose 使用 profiles 但缺少随包 app/docker/.env（安装早期拉起容器时将没有确定的 profile）');
      else if (!/COMPOSE_PROFILES\s*=/.test(readText(dotenvPath)))
        E('随包 .env 未设置 COMPOSE_PROFILES，默认拉起时 profile 不确定');
      else N(`检测到 profiles 部署，默认 profile 由随包 .env 提供: ${appVars.join(', ') || 'COMPOSE_PROFILES'}`);
    }
    if (exists(dotenvPath)) {
      const dotenv = readText(dotenvPath);
      for (const v of appVars) {
        if (!new RegExp(`^${v}\\s*=`, 'm').test(dotenv))
          E(`compose 使用变量 \${${v}}，但随包 .env 未提供默认值（安装早期插值将为空）`);
      }
    }
    if (/^\s*ports:/m.test(yaml) && !/\$\{BIND_PORT\}|\\$\{TRIM_SERVICE_PORT\}/.test(yaml) && !anyHost && mf.service_port)
      W('ports 映射未使用 ${TRIM_SERVICE_PORT} / 变量端口，宿主机端口可能与向导配置脱节');
    if (mf.service_port && anyHost && mf.checkport === 'true')
      N(`host 网络：checkport 将探测宿主机 ${mf.service_port} 端口，确认容器服务监听该端口`);
    // env_file checks: files referenced via ${TRIM_APPDEST} must ship in the package;
    // ${TRIM_PKGVAR} paths may not exist when the stack is first started.
    const envRefs = [...yaml.matchAll(/^\s*-\s*(\$\{(TRIM_[A-Z_]+)\}(\/\S*?\.env))\s*$/gm)];
    for (const [, refPath, trimVar] of envRefs) {
      if (trimVar === 'TRIM_APPDEST') {
        const relInApp = refPath.replace('${TRIM_APPDEST}/', '');
        if (!exists(path.join(dir, 'app', relInApp)))
          E(`compose 引用的 env_file 随包文件缺失: app/${relInApp}（安装拉起容器时会因文件不存在而失败）`);
        else N(`env_file 采用随包携带模式: app/${relInApp}`);
      } else if (trimVar === 'TRIM_PKGVAR') {
        W(
          'compose 引用 $TRIM_PKGVAR 下的 env 文件：该目录在安装拉起容器时可能尚未生成，' +
            '建议改为随包携带（app/docker/env/app.env + ${TRIM_APPDEST}/docker/env/app.env 引用）'
        );
      }
    }
  }

  // ------------------------------------------------------------------- cmd/
  const cmdDir = path.join(dir, 'cmd');
  if (isDir(cmdDir)) {
    const mainFile = path.join(cmdDir, 'main');
    if (!exists(mainFile)) E('缺少 cmd/main 生命周期脚本');
    else {
      const st = fs.statSync(mainFile);
      if (!(st.mode & 0o111)) W('cmd/main 无可执行位（fnpack 安装时会处理，但建议源码中保持 +x）');
      const body = readText(mainFile);
      if (!/status\)/.test(body)) E('cmd/main 缺少 status 分支（应用中心依赖其返回 0/3 判断运行状态）');
      if (/curl[^|]*\|\s*(ba)?sh/.test(body)) W('cmd/main 存在 curl|bash 远程执行模式，禁止在沙箱生命周期脚本中使用');
    }
    for (const f of fs.readdirSync(cmdDir)) {
      const p = path.join(cmdDir, f);
      if (fs.statSync(p).isFile() && /\.(sh|cgi)$/.test(f) && !(fs.statSync(p).mode & 0o111))
        W(`cmd/${f} 无可执行位`);
    }
  }

  // ------------------------------------------------------------------ icons
  const iconSpecs = [
    ['ICON.PNG', 64],
    ['ICON_256.PNG', 256],
  ];
  for (const [f, dim] of iconSpecs) {
    const p = path.join(dir, f);
    if (!exists(p)) continue;
    const info = pngSize(p);
    if (info === 'not-png') E(`${f} 不是合法 PNG`);
    else if (!info) E(`${f} 无法解析`);
    else {
      if (info.width !== dim || info.height !== dim) E(`${f} 尺寸应为 ${dim}x${dim}，实际 ${info.width}x${info.height}`);
      if (info.bytes > 1024 * 1024) E(`${f} 超过 1024KB 限制`);
    }
  }

  // ------------------------------------------------------------------ wizard
  const wizDir = path.join(dir, 'wizard');
  const wizFiles = ['install', 'upgrade', 'uninstall', 'config'];
  if (isDir(wizDir)) {
    for (const name of fs.readdirSync(wizDir)) {
      const p = path.join(wizDir, name);
      if (!fs.statSync(p).isFile()) continue;
      if (!wizFiles.includes(name)) W(`wizard/${name} 不是有效的向导文件名（install/upgrade/uninstall/config）`);
      let steps;
      try {
        steps = JSON.parse(readText(p));
      } catch (e) {
        E(`wizard/${name} 不是合法 JSON: ${e.message}`);
        continue;
      }
      if (!Array.isArray(steps)) {
        E(`wizard/${name} 顶层必须是 JSON 数组（步骤列表）`);
        continue;
      }
      steps.forEach((step, si) => {
        if (!step.stepTitle) W(`wizard/${name}[${si}] 缺少 stepTitle`);
        if (!Array.isArray(step.items)) {
          E(`wizard/${name}[${si}].items 必须是数组`);
          return;
        }
        step.items.forEach((item, ii) => {
          if (item.type && !KNOWN_WIZARD_TYPES.has(item.type))
            E(`wizard/${name}[${si}].items[${ii}] 未知字段类型: ${item.type}`);
          if (item.field) {
            if (String(item.field).startsWith('TRIM_')) E(`wizard/${name}[${si}].items[${ii}] 字段名不得使用保留前缀 TRIM_: ${item.field}`);
            if (!String(item.field).startsWith('wizard_')) N(`向导字段 "${item.field}" 建议使用 wizard_ 前缀`);
            if (!item.label && item.type !== 'tips') W(`wizard/${name}[${si}].items[${ii}] 缺少 label`);
          } else if (item.type !== 'tips') {
            E(`wizard/${name}[${si}].items[${ii}] 缺少 field`);
          }
        });
      });
    }
  }

  return { errors, warnings, notes };
}

module.exports = { checkProject, parseManifest, pngSize };
