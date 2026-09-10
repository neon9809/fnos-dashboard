'use strict';
/**
 * Project templates for `fpksb init` — the fnOS application sandbox layout.
 *
 * Sandbox model (per https://developer.fnnas.com/docs):
 *   - dedicated package user/group      -> config/privilege
 *   - declared resources & docker stack -> config/resource
 *   - isolated dirs etc/var/tmp/home    -> referenced only via TRIM_* variables
 *   - lifecycle scripts                 -> cmd/*
 */

function manifest(o) {
  return [
    `appname               = ${o.appName}`,
    `version               = ${o.version || '1.0.0'}`,
    `display_name          = ${o.displayName}`,
    `desc                  = ${o.desc}`,
    'source                = thirdparty',
    `platform              = ${o.platform || 'all'}`,
    `maintainer            = ${o.maintainer}`,
    `maintainer_url        = ${o.maintainerUrl || ''}`,
    'desktop_uidir         = ui',
    `desktop_applaunchname = ${o.appName}.main`,
    o.servicePort ? `service_port          = ${o.servicePort}` : null,
    o.servicePort ? 'checkport             = true' : 'checkport             = false',
    'ctl_stop              = true',
  ]
    .filter(Boolean)
    .join('\n') + '\n';
}

function privilege(o) {
  return (
    JSON.stringify(
      {
        defaults: { 'run-as': 'package' },
        username: o.appName,
        groupname: o.appName,
      },
      null,
      2
    ) + '\n'
  );
}

function resource(o) {
  const res = {
    'docker-project': {
      projects: [{ name: o.appName, path: 'docker' }],
    },
  };
  if (o.dataShare) {
    res['data-share'] = { shares: [{ name: `${o.appName}/data` }] };
  }
  return JSON.stringify(res, null, 2) + '\n';
}

function uiConfig(o) {
  return (
    JSON.stringify(
      {
        '.url': {
          [`${o.appName}.main`]: {
            title: o.displayName,
            icon: 'images/icon_{0}.png',
            type: 'iframe',
            protocol: 'http',
            port: String(o.servicePort || 8080),
            url: '/',
            allUsers: true,
          },
        },
      },
      null,
      2
    ) + '\n'
  );
}

/**
 * docker-compose.yaml for the app stack.
 * net: 'host' keeps host networking (needed for raw IPv4/IPv6 probing);
 * net: 'bridge' maps ${TRIM_SERVICE_PORT} to the container port.
 *
 * Runtime parameters use an env file SHIPPED INSIDE the package
 * (${TRIM_APPDEST}/docker/env/app.env): it exists as soon as files are
 * applied, so `docker compose up` can never fail on a missing env_file —
 * even if lifecycle callbacks have not run yet.
 */
function compose(o) {
  const svc = {
    image: o.image,
    container_name: o.containerName || o.appName,
    restart: 'unless-stopped',
    logging: { driver: 'json-file', options: { 'max-size': '10m', 'max-file': '3' } },
  };
  if (o.net === 'host') {
    svc.network_mode = 'host';
  } else {
    svc.ports = ['${TRIM_SERVICE_PORT}:' + (o.containerPort || o.servicePort || 8080)];
  }
  if (o.envFile !== false) {
    svc.env_file = ['${TRIM_APPDEST}/docker/env/app.env'];
  }
  if (o.dataMount) {
    svc.volumes = ['${TRIM_PKGVAR}/data:' + o.dataMount];
  }
  const lines = ['services:', `  ${o.appName}:`];
  lines.push(`    image: ${svc.image}`);
  lines.push(`    container_name: ${svc.container_name}`);
  lines.push('    restart: unless-stopped');
  if (svc.network_mode) lines.push(`    network_mode: ${svc.network_mode}`);
  if (svc.ports) lines.push('    ports:', ...svc.ports.map((p) => `      - "${p}"`));
  if (svc.env_file)
    lines.push(
      '    # 运行参数文件随包携带，安装后必然存在；向导值由生命周期脚本同步覆盖',
      '    env_file:',
      ...svc.env_file.map((p) => `      - ${p}`)
    );
  if (svc.volumes) lines.push('    volumes:', ...svc.volumes.map((v) => `      - ${v}`));
  lines.push('    logging:', `      driver: ${svc.logging.driver}`, '      options:', `        max-size: "${svc.logging.options['max-size']}"`, `        max-file: "${svc.logging.options['max-file']}"`);
  return lines.join('\n') + '\n';
}

/** Default shipped env file referenced by the compose env_file above. */
function shippedEnv(o) {
  return [
    '# 运行参数默认值（随应用包携带；安装后位于 ${TRIM_APPDEST}/docker/env/app.env）。',
    '# 如需安装时收集用户输入，请在 wizard/ 中定义字段，并在 cmd 回调里把值写入本文件，',
    '# 主副本保存在 ${TRIM_PKGETC}/app.env 以便升级保留。',
    ...(o.defaultEnv || []),
    '',
  ].join('\n');
}

const CMD_MAIN = (containerName, extraStart) => `#!/bin/bash
# fnOS lifecycle: start / stop / status.
# Docker stacks are brought up and down by appcenter through config/resource;
# this script only guarantees sandbox prerequisites and reports status.

CONTAINER_NAME="${containerName}"
ENV_FILE="\${TRIM_PKGVAR}/env/app.env"

ensure_env_file() {
  # The compose file references \${TRIM_PKGVAR}/env/app.env; make sure it exists
  # before the first start so that 'docker compose up' cannot fail on it.
  if [ ! -f "$ENV_FILE" ]; then
    mkdir -p "$(dirname "$ENV_FILE")"
    : > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
  fi
}

is_running() {
  docker inspect "$CONTAINER_NAME" 2>/dev/null | grep -q '"Status": "running"'
}

case "$1" in
start)
${extraStart || '    ensure_env_file'}
    exit 0
    ;;
stop)
    exit 0
    ;;
status)
    if is_running; then
        exit 0
    fi
    exit 3
    ;;
*)
    echo "Unknown command: $1" > "$TRIM_TEMP_LOGFILE"
    exit 1
    ;;
esac
`;

const CMD_STUB = (comment) => `#!/bin/bash

### ${comment}

exit 0
`;

module.exports = { manifest, privilege, resource, uiConfig, compose, shippedEnv, CMD_MAIN, CMD_STUB };
