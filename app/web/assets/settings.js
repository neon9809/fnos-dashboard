/* 设置页逻辑：左配置右预览，保存后 POST /api/settings 同步到显示屏 */
"use strict";

var THEMES = [
  { id: "midnight", name: "午夜蓝", bg: "#0a1322", accent: "#3b82f6" },
  { id: "graphite", name: "石墨黑", bg: "#171b22", accent: "#2dd4bf" },
  { id: "emerald",  name: "翡翠绿", bg: "#0b3226", accent: "#34d399" },
  { id: "solar",    name: "日光橙", bg: "#2b1c08", accent: "#f59e0b" },
  { id: "sakura",   name: "樱粉",   bg: "#ffffff", accent: "#ec4899" },
  { id: "light",    name: "云白",   bg: "#ffffff", accent: "#2563eb" }
];
var PRESET_ACCENTS = [
  "#3b82f6", "#22d3ee", "#2dd4bf", "#34d399", "#a3e635", "#facc15",
  "#f59e0b", "#fb7185", "#ec4899", "#a78bfa", "#f43f5e", "#94a3b8"
];
var CARD_LABELS = {
  cpu: "CPU", memory: "内存", disk: "存储空间",
  network: "网络", temperature: "温度", info: "系统信息"
};
var VERIFY_BADGE = {
  verified: ["verified", "✓ 已验证"],
  untrusted: ["untrusted", "⚠ 未知签名者"],
  unsigned: ["unsigned", "未签名"]
};

var state = null;   // 可编辑配置副本
var previewTimer = null;

function api(method, url, body, raw) {
  var opts = { method: method };
  if (body !== undefined) {
    if (raw) { opts.body = body; }
    else {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
  }
  return fetch(url, opts).then(function (r) {
    return r.json().catch(function () { throw new Error("HTTP " + r.status); })
      .then(function (j) {
        if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
  });
}

function toast(msg, ok) {
  var el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(function () { el.classList.add("hidden"); }, 2600);
}

function riskConfirm() {
  return new Promise(function (resolve) {
    var mask = document.getElementById("risk-mask");
    mask.classList.remove("hidden");
    function done(v) {
      mask.classList.add("hidden");
      document.getElementById("risk-ok").onclick = null;
      document.getElementById("risk-cancel").onclick = null;
      resolve(v);
    }
    document.getElementById("risk-ok").onclick = function () { done(true); };
    document.getElementById("risk-cancel").onclick = function () { done(false); };
  });
}

/* ---------------- 主题 ---------------- */
function renderThemes() {
  var grid = document.getElementById("theme-grid");
  grid.textContent = "";
  THEMES.forEach(function (t) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "theme-card" + (state.theme === t.id ? " active" : "");
    var prev = document.createElement("div");
    prev.className = "theme-preview";
    prev.style.background = t.bg;
    prev.style.setProperty("--tc-accent", state.accent || t.accent);
    var name = document.createElement("span");
    name.className = "theme-name";
    name.textContent = t.name;
    btn.appendChild(prev);
    btn.appendChild(name);
    btn.addEventListener("click", function () {
      state.theme = t.id;
      renderThemes();
      renderSwatches();
    });
    grid.appendChild(btn);
  });
}

function renderSwatches() {
  var wrap = document.getElementById("accent-swatches");
  wrap.textContent = "";
  PRESET_ACCENTS.forEach(function (color) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "swatch" + (state.accent === color ? " active" : "");
    b.style.setProperty("--sw", color);
    b.title = color;
    b.addEventListener("click", function () {
      state.accent = color;
      renderSwatches();
    });
    wrap.appendChild(b);
  });
  document.getElementById("accent-picker").value = state.accent || themeAccent(state.theme);
}

function themeAccent(id) {
  for (var i = 0; i < THEMES.length; i++) if (THEMES[i].id === id) return THEMES[i].accent;
  return "#3b82f6";
}

/* ---------------- 显示 ---------------- */
function renderCards() {
  var wrap = document.getElementById("card-toggles");
  wrap.textContent = "";
  Object.keys(CARD_LABELS).forEach(function (k) {
    var row = document.createElement("label");
    row.className = "toggle-row";
    var span = document.createElement("span");
    span.textContent = CARD_LABELS[k];
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!state.cards[k];
    cb.addEventListener("change", function () { state.cards[k] = cb.checked; });
    row.appendChild(span);
    row.appendChild(cb);
    wrap.appendChild(row);
  });
}

function renderSeg() {
  document.querySelectorAll("#seg-rotate button").forEach(function (b) {
    b.classList.toggle("active", parseInt(b.dataset.v, 10) === state.fb_rotate);
  });
}

/* ---------------- 显示器 ---------------- */
function renderFbInfo(info) {
  var el = document.getElementById("fb-info");
  if (!info || !info.exists) {
    el.innerHTML = "未检测到 <b>/dev/fb0</b>（当前环境无帧缓冲输出设备）。";
    return;
  }
  var lines = [];
  lines.push("分辨率 <b>" + info.w + " × " + info.h + "</b> @ " + info.bpp + "bpp");
  lines.push("中文渲染 PIL：" + (info.pil
    ? '<span class="ok">已安装</span>'
    : '<span class="bad">未安装</span>（apt install python3-pil）'));
  lines.push("渲染进程：" + (info.renderer_running
    ? '<span class="ok">运行中</span>'
    : '<span class="bad">未运行</span>（保存后重启应用生效）'));
  el.innerHTML = lines.join("<br>");
}

/* ---------------- 模组 ---------------- */
function bindBuiltinMods() {
  document.getElementById("mod-calendar").checked = !!state.modules.calendar;
  document.getElementById("mod-weather").checked = !!state.modules.weather;
  document.getElementById("mod-weather").checked = !!state.modules.weather;
  document.getElementById("set-city").value = state.weather_city;
  document.getElementById("set-wprovider").value = state.weather_provider || "open-meteo";
  document.getElementById("set-qhost").value = state.qweather_host || "devapi.qweather.com";
  document.getElementById("set-qkey").value = state.qweather_key ? QWEATHER_MASK : "";
  document.getElementById("set-wloc").value = state.weather_location;
  renderIndexCheckboxes();
}

function renderIndexCheckboxes() {
  var wrap = document.getElementById("idx-checkboxes");
  if (!wrap) return;
  wrap.textContent = "";
  var enabled = (state.qweather_indices || "7").split(",").filter(Boolean);
  QWEATHER_INDICES.forEach(function (idx) {
    var lab = document.createElement("label");
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = enabled.indexOf(idx.id) >= 0;
    cb.addEventListener("change", function () {
      var set = new Set((state.qweather_indices || "").split(",").filter(Boolean));
      if (cb.checked) set.add(idx.id); else set.delete(idx.id);
      state.qweather_indices = Array.from(set).join(",") || "7";
    });
    var span = document.createElement("span");
    span.textContent = idx.name;
    lab.appendChild(cb);
    lab.appendChild(span);
    wrap.appendChild(lab);
  });
}

function renderExtModules() {
  var wrap = document.getElementById("ext-list");
  wrap.textContent = "";
  api("GET", "/api/ext/modules").then(function (data) {
    var list = data.modules || [];
    if (!list.length) {
      var empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "尚未安装扩展模组。点击「安装示例模组」体验，或参阅 MODULE_SPEC.md 自行开发。";
      wrap.appendChild(empty);
      return;
    }
    list.forEach(function (m) {
      var item = document.createElement("div");
      item.className = "ext-item";

      var head = document.createElement("div");
      head.className = "ext-head";
      var name = document.createElement("span");
      name.className = "ext-name";
      name.textContent = m.name + "（" + m.id + " v" + m.version + "）";
      var meta = document.createElement("div");
      meta.className = "ext-meta";
      meta.textContent = (m.author ? "作者 " + m.author + " · " : "")
        + (m.desc || "");
      head.appendChild(name);
      var v = m.verify || {};
      var badge = VERIFY_BADGE[v.status] || ["unsigned", "未签名"];
      var badgeEl = document.createElement("span");
      badgeEl.className = "badge " + badge[0];
      badgeEl.title = v.signer_id ? "签名者 " + v.signer_id + "（" + (v.key_id || "") + "）" : "";
      badgeEl.textContent = badge[1];
      head.appendChild(badgeEl);
      item.appendChild(head);
      item.appendChild(meta);

      var cfgBox = document.createElement("div");
      cfgBox.className = "ext-config";
      var toggleRow = document.createElement("label");
      toggleRow.className = "toggle-row";
      var tSpan = document.createElement("span");
      tSpan.textContent = "启用（加入轮换）";
      var tCb = document.createElement("input");
      tCb.type = "checkbox";
      tCb.checked = !!m.enabled;
      tCb.addEventListener("change", function () {
        if (!tCb.checked) {
          state.ext_modules[m.id] = { enabled: false, config: m.config || {} };
          save();
          return;
        }
        riskConfirm().then(function (yes) {
          if (!yes) { tCb.checked = false; return; }
          state.ext_modules[m.id] = { enabled: true, config: m.config || {} };
          save();
        });
      });
      toggleRow.appendChild(tSpan);
      toggleRow.appendChild(tCb);
      cfgBox.appendChild(toggleRow);

      (m.config_schema || []).forEach(function (f) {
        if (!f.field) return;
        var row = document.createElement("label");
        row.className = "field";
        var lab = document.createElement("span");
        lab.textContent = f.label || f.field;
        var input;
        if (f.type === "select" && Array.isArray(f.options)) {
          input = document.createElement("select");
          input.className = "text-input";
          f.options.forEach(function (op) {
            var opt = document.createElement("option");
            opt.value = op.value;
            opt.textContent = op.label || op.value;
            input.appendChild(opt);
          });
        } else {
          input = document.createElement("input");
          input.className = "text-input";
          input.type = f.type === "password" ? "password"
            : f.type === "number" ? "number" : "text";
        }
        var cur = (m.config || {})[f.field];
        input.value = cur === undefined ? (f.default == null ? "" : f.default) : cur;
        input.addEventListener("change", function () {
          var conf = (state.ext_modules[m.id] || (state.ext_modules[m.id] = {
            enabled: !!m.enabled, config: {}
          })).config;
          conf[f.field] = f.type === "number"
            ? (parseFloat(input.value) || 0) : input.value;
        });
        row.appendChild(lab);
        row.appendChild(input);
        cfgBox.appendChild(row);
      });

      var del = document.createElement("button");
      del.className = "btn-mini danger-mini";
      del.textContent = "卸载";
      del.addEventListener("click", function () {
        if (!confirm("卸载模组 " + m.id + "？")) return;
        api("POST", "/api/ext/uninstall", { id: m.id }).then(function () {
          toast("已卸载 " + m.id, true);
          renderExtModules();
        }).catch(function (e) { toast("卸载失败：" + e.message, false); });
      });
      cfgBox.appendChild(del);
      item.appendChild(cfgBox);
      wrap.appendChild(item);
    });
  }).catch(function (e) {
    var err = document.createElement("p");
    err.className = "hint";
    err.textContent = "模组列表加载失败：" + e.message;
    wrap.appendChild(err);
  });
}

function installExt(bytes, filename) {
  return riskConfirm().then(function (yes) {
    if (!yes) return null;
    return api("POST", "/api/ext/install?filename=" + encodeURIComponent(filename),
               bytes, true)
      .then(function (r) {
        var v = (r.manifest || {})._verify || {};
        toast("安装成功（" + (VERIFY_BADGE[v.status] || ["", v.status || "未签名"])[1] + "）", true);
        renderExtModules();
      });
  });
}

function renderKeys() {
  var wrap = document.getElementById("key-list");
  wrap.textContent = "";
  api("GET", "/api/ext/keys").then(function (data) {
    var keys = data.keys || [];
    if (!keys.length) {
      var p = document.createElement("p");
      p.className = "hint";
      p.textContent = "尚未添加信任密钥。签名者的公钥加入后，其模组将显示「✓ 已验证」。";
      wrap.appendChild(p);
      return;
    }
    keys.forEach(function (k) {
      var item = document.createElement("div");
      item.className = "key-item";
      var name = document.createElement("b");
      name.textContent = k.name || "未命名";
      var code = document.createElement("code");
      code.textContent = k.key_id;
      var del = document.createElement("button");
      del.className = "btn-mini";
      del.textContent = "移除";
      del.addEventListener("click", function () {
        api("DELETE", "/api/ext/keys", { key_id: k.key_id })
          .then(renderKeys)
          .catch(function (e) { toast("移除失败：" + e.message, false); });
      });
      item.appendChild(name);
      item.appendChild(code);
      item.appendChild(del);
      wrap.appendChild(item);
    });
  });
}

/* ---------------- 保存 ---------------- */
function save() {
  var btn = document.getElementById("btn-save");
  var st = document.getElementById("save-state");
  btn.disabled = true;
  st.className = "";
  st.textContent = "保存中…";
  api("POST", "/api/settings", {
    theme: state.theme,
    accent: state.accent,
    refresh: state.refresh,
    temp_unit: state.temp_unit,
    cards: state.cards,
    fb_enabled: state.fb_enabled,
    rotate_seconds: state.rotate_seconds,
    screen_inches: state.screen_inches,
    fb_rotate: state.fb_rotate,
    modules: state.modules,
    weather_city: state.weather_city,
    weather_provider: state.weather_provider,
    qweather_host: state.qweather_host,
    qweather_key: state.qweather_key,
    weather_location: state.weather_location,
    qweather_indices: state.qweather_indices,
    ext_modules: state.ext_modules
  }).then(function () {
    st.className = "ok";
    st.textContent = "已保存，约 2 秒内同步到显示屏";
    toast("已同步到显示屏", true);
    refreshPreview();
  }).catch(function (e) {
    st.className = "err";
    st.textContent = "保存失败：" + e.message;
  }).finally(function () {
    btn.disabled = false;
    setTimeout(function () { st.textContent = ""; }, 4000);
  });
}

/* ---------------- 预览 ---------------- */
function refreshPreview() {
  var img = document.getElementById("fb-preview");
  var ph = document.getElementById("preview-placeholder");
  img.src = "/api/fb/dump.png?t=" + Date.now();
  img.onload = function () { ph.classList.add("hidden"); img.classList.remove("hidden"); };
  img.onerror = function () { img.classList.add("hidden"); ph.classList.remove("hidden"); };
}

function restartPreviewTimer() {
  clearInterval(previewTimer);
  if (document.getElementById("preview-auto").checked) {
    previewTimer = setInterval(refreshPreview, 3000);
  }
}

/* ---------------- 初始化 ---------------- */
function bindUI() {
  document.querySelectorAll("#tabs button").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll("#tabs button").forEach(function (x) { x.classList.remove("active"); });
      document.querySelectorAll(".tab").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active");
      document.getElementById("tab-" + b.dataset.tab).classList.add("active");
    });
  });

  document.getElementById("accent-picker").addEventListener("input", function () {
    state.accent = this.value;
    renderSwatches();
  });
  document.getElementById("accent-reset").addEventListener("click", function () {
    state.accent = "";
    renderSwatches();
  });
  document.getElementById("set-temp-unit").addEventListener("change", function () {
    state.temp_unit = this.value;
  });
  document.getElementById("set-refresh").addEventListener("change", function () {
    state.refresh = parseInt(this.value, 10) || 2;
  });
  document.getElementById("set-rotate").addEventListener("change", function () {
    state.rotate_seconds = parseInt(this.value, 10) || 15;
  });
  document.querySelectorAll("#seg-rotate button").forEach(function (b) {
    b.addEventListener("click", function () {
      state.fb_rotate = parseInt(b.dataset.v, 10);
      renderSeg();
    });
  });
  document.getElementById("set-fb").addEventListener("change", function () {
    state.fb_enabled = this.checked;
  });
  document.getElementById("set-inches").addEventListener("change", function () {
    state.screen_inches = parseFloat(this.value) || 0;
  });
  document.getElementById("mod-calendar").addEventListener("change", function () {
    state.modules.calendar = this.checked;
  });
  document.getElementById("mod-weather").addEventListener("change", function () {
    state.modules.weather = this.checked;
  });
  [["set-city", function (v) { state.weather_city = v; }]
  ].forEach(function (pair) {
    document.getElementById(pair[0]).addEventListener("change", function () {
      pair[1](this.value.trim());
    });
  });

  document.getElementById("file-ext").addEventListener("change", function () {
    var f = this.files[0];
    this.value = "";
    if (!f) return;
    f.arrayBuffer().then(function (buf) {
      return installExt(new Uint8Array(buf), f.name);
    }).then(function (installed) {
      if (installed === false || installed === null) return;
    }).catch(function (e) { toast("安装失败：" + e.message, false); });
  });

  document.getElementById("btn-key-add").addEventListener("click", function () {
    var name = document.getElementById("key-name").value.trim();
    var pub = document.getElementById("key-public").value.trim();
    if (!pub) { toast("请粘贴公钥", false); return; }
    api("POST", "/api/ext/keys", { name: name, public_key: pub })
      .then(function () {
        toast("信任密钥已添加", true);
        document.getElementById("key-name").value = "";
        document.getElementById("key-public").value = "";
        renderKeys();
      }).catch(function (e) { toast("添加失败：" + e.message, false); });
  });

  document.getElementById("btn-save").addEventListener("click", save);
  document.getElementById("preview-refresh").addEventListener("click", refreshPreview);
  document.getElementById("preview-auto").addEventListener("change", restartPreviewTimer);
}

function boot() {
  api("GET", "/api/config").then(function (data) {
    state = JSON.parse(JSON.stringify(data.config));
    renderThemes();
    renderSwatches();
    renderCards();
    document.getElementById("set-refresh").value = String(state.refresh);
    document.getElementById("set-rotate").value = String(state.rotate_seconds);
    document.getElementById("set-temp-unit").value = state.temp_unit;
    document.getElementById("set-fb").checked = !!state.fb_enabled;
    document.getElementById("set-inches").value = String(state.screen_inches);
    renderSeg();
    bindBuiltinMods();
    renderExtModules();
    renderKeys();
  }).catch(function (e) {
    toast("配置加载失败：" + e.message, false);
  });

  api("GET", "/api/fb/info").then(function (data) {
    renderFbInfo(data.fb);
  });

  bindUI();
  refreshPreview();
  restartPreviewTimer();
}

boot();
