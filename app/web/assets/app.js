/* fnOS 状态监视 —— 面板（纯查看端）
   参考 lcdsimple 的「采集快照 + 定时刷新 + 主题渲染」模式。
   所有显示配置在 /settings（仅限 NAS 本机）管理，面板只跟随配置渲染。 */
"use strict";

var THEMES = [
  { id: "midnight", name: "午夜蓝", bg: "#0a1322", accent: "#3b82f6" },
  { id: "graphite", name: "石墨黑", bg: "#171b22", accent: "#2dd4bf" },
  { id: "emerald",  name: "翡翠绿", bg: "#0b3226", accent: "#34d399" },
  { id: "solar",    name: "日光橙", bg: "#2b1c08", accent: "#f59e0b" },
  { id: "sakura",   name: "樱粉",   bg: "#ffffff", accent: "#ec4899" },
  { id: "light",    name: "云白",   bg: "#ffffff", accent: "#2563eb" }
];

var cfg = { theme: "midnight", accent: "", refresh: 2, temp_unit: "C",
            cards: { cpu: true, memory: true, disk: true, network: true,
                     temperature: true, info: true } };
var timer = null;
var cpuHist = [], downHist = [], upHist = [];
var lastStats = null;
var coreEls = [];
var HIST_LEN = 90;
var sparkDirty = false;

/* ---------------- API ---------------- */
function poll() {
  fetch("/api/status").then(function (r) {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }).then(function (data) {
    hideOffline();
    var c = data.config || {};
    if (c.theme !== cfg.theme || c.accent !== cfg.accent ||
        JSON.stringify(c.cards) !== JSON.stringify(cfg.cards) ||
        c.temp_unit !== cfg.temp_unit) {
      cfg = normalizeCfg(c);
      applyConfig();
    }
    lastStats = data.stats;
    render(data.stats);
    sparkDirty = true;
  }).catch(function () { showOffline(); });
}

function normalizeCfg(c) {
  c = c && typeof c === "object" ? c : {};
  var ok = false;
  for (var i = 0; i < THEMES.length; i++) if (THEMES[i].id === c.theme) ok = true;
  if (!ok) c.theme = "midnight";
  if (typeof c.accent !== "string" || !/^#[0-9a-fA-F]{6}$/.test(c.accent)) c.accent = "";
  c.refresh = Math.min(60, Math.max(1, parseInt(c.refresh, 10) || 2));
  if (c.temp_unit !== "C" && c.temp_unit !== "F") c.temp_unit = "C";
  var cards = { cpu: true, memory: true, disk: true, network: true,
                temperature: true, info: true };
  if (c.cards) for (var k in cards) if (k in c.cards) cards[k] = !!c.cards[k];
  c.cards = cards;
  return c;
}

function restartTimer() {
  if (timer) clearInterval(timer);
  timer = setInterval(poll, cfg.refresh * 1000);
}

function applyConfig() {
  document.body.dataset.theme = cfg.theme;
  if (cfg.accent) document.body.style.setProperty("--accent", cfg.accent);
  else document.body.style.removeProperty("--accent");
  for (var k in cfg.cards) {
    var el = document.getElementById("card-" + k);
    if (el) el.classList.toggle("hidden", !cfg.cards[k]);
  }
}

/* ---------------- 格式化 ---------------- */
function fmtBytes(n) {
  if (n == null || isNaN(n)) return "--";
  var u = ["B", "KB", "MB", "GB", "TB", "PB"], i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  var s = v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
  return s + " " + u[i];
}
function fmtRate(n) { return fmtBytes(n) + "/s"; }
function fmtUptime(s) {
  if (s == null) return "--";
  s = Math.floor(s);
  var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return d + " 天 " + h + " 小时";
  if (h > 0) return h + " 小时 " + m + " 分";
  return m + " 分钟";
}
function fmtTemp(c) {
  if (c == null) return "--";
  return cfg.temp_unit === "F" ? (c * 9 / 5 + 32).toFixed(1) : c.toFixed(1);
}
function tempUnitStr() { return cfg.temp_unit === "F" ? "℉" : "℃"; }
function pctClass(p) { return p >= 85 ? "danger" : p >= 60 ? "warn" : ""; }

function setBig(id, val, cls) {
  var el = document.getElementById(id);
  if (!el) return;
  el.textContent = val == null ? "--" : val;
  el.className = cls || "";
}

/* ---------------- 渲染 ---------------- */
function renderCores(cores) {
  var grid = document.getElementById("cpu-cores");
  if (!grid) return;
  if (coreEls.length !== cores.length) {
    grid.textContent = "";
    coreEls = [];
    cores.forEach(function () {
      var c = document.createElement("div");
      c.className = "core";
      var f = document.createElement("div");
      f.className = "core-fill";
      f.style.height = "0%";
      c.appendChild(f);
      grid.appendChild(c);
      coreEls.push(f);
    });
  }
  cores.forEach(function (u, i) {
    var el = coreEls[i];
    if (el) el.style.height = Math.max(2, Math.min(100, u)) + "%";
  });
}

function renderDisks(disks) {
  var list = document.getElementById("disk-list");
  var count = document.getElementById("disk-count");
  if (!list) return;
  if (count) count.textContent = disks.length ? disks.length + " 个挂载点" : "未检测到";
  list.textContent = "";
  disks.forEach(function (d) {
    var vol = document.createElement("div");
    vol.className = "vol";
    var top = document.createElement("div");
    top.className = "vol-top";
    var mount = document.createElement("span");
    mount.className = "vol-mount";
    mount.textContent = d.mount;
    var detail = document.createElement("span");
    detail.className = "vol-detail";
    detail.textContent = fmtBytes(d.used) + " / " + fmtBytes(d.total) + (d.ro ? "（只读）" : "");
    top.appendChild(mount);
    top.appendChild(detail);
    var bar = document.createElement("div");
    bar.className = "bar";
    var fill = document.createElement("div");
    fill.className = "bar-fill" + (d.usage >= 85 ? " warn" : "");
    fill.style.width = Math.min(100, d.usage) + "%";
    bar.appendChild(fill);
    vol.appendChild(top);
    vol.appendChild(bar);
    list.appendChild(vol);
  });
}

function primaryIface(net) {
  var list = (net && net.ifaces) || [];
  var best = null;
  for (var i = 0; i < list.length; i++) {
    var it = list[i];
    var score = it.rx_rate + it.tx_rate;
    if (!best || score > best._score) { best = it; best._score = score; }
  }
  return best || (list[0] || null);
}

function renderNetTable(net) {
  var tb = document.getElementById("net-table");
  if (!tb) return;
  tb.textContent = "";
  var list = (net && net.ifaces) || [];
  if (!list.length) return;
  var thead = document.createElement("tr");
  ["网卡", "下载", "上传", "累计收", "累计发"].forEach(function (t) {
    var th = document.createElement("th");
    th.textContent = t;
    thead.appendChild(th);
  });
  tb.appendChild(thead);
  list.forEach(function (it) {
    var tr = document.createElement("tr");
    [it.name, fmtRate(it.rx_rate), fmtRate(it.tx_rate), fmtBytes(it.rx), fmtBytes(it.tx)]
      .forEach(function (v, idx) {
        var td = document.createElement("td");
        if (idx > 0) td.className = idx < 3 ? "" : "dim";
        td.textContent = v;
        tr.appendChild(td);
      });
    tb.appendChild(tr);
  });
}

function renderInfo(h) {
  var grid = document.getElementById("info-grid");
  if (!grid) return;
  grid.textContent = "";
  [["系统", h.os || "--"], ["内核", h.kernel || "--"], ["架构", h.arch || "--"],
   ["CPU", h.cpu_model || "--"], ["进程数", h.procs == null ? "--" : String(h.procs)],
   ["运行时间", fmtUptime(h.uptime)]
  ].forEach(function (r) {
    var k = document.createElement("span");
    k.className = "k";
    k.textContent = r[0];
    var v = document.createElement("span");
    v.className = "v";
    v.textContent = r[1];
    grid.appendChild(k);
    grid.appendChild(v);
  });
}

function renderTemps(temps) {
  var chips = document.getElementById("temp-chips");
  var count = document.getElementById("temp-count");
  if (!chips) return;
  var sensors = (temps && temps.sensors) || [];
  if (count) count.textContent = sensors.length ? sensors.length + " 个传感器" : "未检测到传感器";
  chips.textContent = "";
  if (!sensors.length) {
    var empty = document.createElement("span");
    empty.className = "kv";
    empty.textContent = "未在 hwmon / thermal 中发现温度传感器";
    chips.appendChild(empty);
    return;
  }
  sensors.forEach(function (s) {
    var chip = document.createElement("div");
    chip.className = "chip" + (s.celsius >= 80 ? " danger" : s.celsius >= 60 ? " warn" : "");
    var label = document.createElement("span");
    label.className = "chip-label";
    label.textContent = s.label;
    label.title = s.label + "（" + s.chip + "）";
    var val = document.createElement("span");
    val.className = "chip-val";
    val.textContent = fmtTemp(s.celsius) + " " + tempUnitStr();
    chip.appendChild(label);
    chip.appendChild(val);
    chips.appendChild(chip);
  });
}

function render(stats) {
  if (!stats) return;

  var host = stats.host || {};
  var hostName = document.getElementById("host-name");
  if (hostName) hostName.textContent = host.hostname || "";
  var hostOs = document.getElementById("host-os");
  if (hostOs) hostOs.textContent = host.os || "";

  var cpu = stats.cpu || {};
  pushHist(cpuHist, cpu.usage || 0);
  setBig("cpu-usage", cpu.usage == null ? "--" : cpu.usage.toFixed(1), pctClass(cpu.usage));
  var load = document.getElementById("cpu-load");
  if (load && cpu.load) load.textContent = "负载 " + cpu.load[0].toFixed(2) + " / " + cpu.load[1].toFixed(2);
  renderCores(cpu.cores || []);

  var mem = stats.memory || {};
  setBig("mem-usage", mem.total ? mem.usage.toFixed(1) : "--", pctClass(mem.usage));
  var memBar = document.getElementById("mem-bar");
  if (memBar) memBar.style.width = Math.min(100, mem.usage || 0) + "%";
  var memDetail = document.getElementById("mem-detail");
  if (memDetail) memDetail.textContent = fmtBytes(mem.used) + " / " + fmtBytes(mem.total);
  var memSwap = document.getElementById("mem-swap");
  if (memSwap) memSwap.textContent = mem.swap_total
    ? "Swap " + (mem.swap_used / mem.swap_total * 100).toFixed(1) + "%"
    : "无 Swap";

  renderDisks(stats.disks || []);

  var net = stats.net || {};
  var primary = primaryIface(net);
  var down = primary ? primary.rx_rate : 0;
  var up = primary ? primary.tx_rate : 0;
  pushHist(downHist, down);
  pushHist(upHist, up);
  var netDown = document.getElementById("net-down");
  if (netDown) netDown.textContent = fmtRate(down);
  var netUp = document.getElementById("net-up");
  if (netUp) netUp.textContent = fmtRate(up);
  var netIface = document.getElementById("net-iface");
  if (netIface) netIface.textContent = primary ? primary.name : "无活动网卡";
  renderNetTable(net);

  renderTemps(stats.temps);
  renderInfo(host);
}

function pushHist(arr, v) {
  arr.push(v);
  if (arr.length > HIST_LEN) arr.shift();
}

/* ---------------- 迷你趋势图 ---------------- */
function cssVar(name, fallback) {
  var v = getComputedStyle(document.body).getPropertyValue(name).trim();
  return v || fallback;
}

function drawSpark(canvas, series, opts) {
  opts = opts || {};
  var dpr = window.devicePixelRatio || 1;
  var w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  var ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  var max = opts.max;
  if (max == null) {
    max = 1;
    series.forEach(function (s) {
      for (var i = 0; i < s.data.length; i++) if (s.data[i] > max) max = s.data[i];
    });
    max *= 1.15;
  }

  ctx.strokeStyle = cssVar("--card-brd", "rgba(128,128,128,.2)");
  ctx.lineWidth = 1;
  [0.25, 0.5, 0.75].forEach(function (fy) {
    ctx.beginPath();
    ctx.moveTo(0, h * fy);
    ctx.lineTo(w, h * fy);
    ctx.stroke();
  });

  series.forEach(function (s) {
    var data = s.data;
    if (data.length < 2) return;
    var step = w / (HIST_LEN - 1);
    var x0 = w - (data.length - 1) * step;
    function pt(i) {
      return [x0 + i * step, h - Math.min(1, data[i] / max) * (h - 4) - 2];
    }
    ctx.beginPath();
    for (var i = 0; i < data.length; i++) {
      var p = pt(i);
      if (i === 0) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
    }
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.stroke();
    ctx.lineTo(x0 + (data.length - 1) * step, h);
    ctx.lineTo(x0, h);
    ctx.closePath();
    ctx.fillStyle = s.fill;
    ctx.fill();
  });
}

function drawSparks() {
  var cpu = document.getElementById("cpu-spark");
  if (cpu) {
    var a = cssVar("--accent", "#3b82f6");
    drawSpark(cpu, [{ data: cpuHist, color: a, fill: hexToRgba(a, .15) }], { max: 100 });
  }
  var net = document.getElementById("net-spark");
  if (net) {
    var dc = cssVar("--down", "#34d399");
    var uc = cssVar("--up", "#fbbf24");
    var max = 1;
    downHist.concat(upHist).forEach(function (v) { if (v > max) max = v; });
    drawSpark(net, [
      { data: downHist, color: dc, fill: hexToRgba(dc, .13) },
      { data: upHist, color: uc, fill: hexToRgba(uc, .13) }
    ], { max: max * 1.15 });
  }
  sparkDirty = false;
}

function hexToRgba(hex, alpha) {
  var m = /^#?([0-9a-fA-F]{6})$/.exec(hex);
  if (!m) return "rgba(128,128,128," + alpha + ")";
  var n = parseInt(m[1], 16);
  return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + alpha + ")";
}

/* ---------------- 其他 UI ---------------- */
function showOffline() { var el = document.getElementById("offline"); if (el) el.classList.remove("hidden"); }
function hideOffline() { var el = document.getElementById("offline"); if (el) el.classList.add("hidden"); }

function tickClock() {
  var el = document.getElementById("clock");
  if (el) el.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function boot() {
  tickClock();
  setInterval(tickClock, 1000);
  window.addEventListener("resize", function () { if (lastStats) drawSparks(); });

  poll();
  restartTimer();
  setInterval(function () { if (lastStats && sparkDirty) drawSparks(); }, 1000);
}

boot();
