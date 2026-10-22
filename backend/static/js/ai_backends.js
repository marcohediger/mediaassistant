/* Settings page — AI backend list and its popup.
 *
 * The list (#ai-table, templates/settings/_ai.html) shows both backend slots
 * with a live status; the popup (#dlg-ai, _ai_dialog.html, outside the big
 * form) edits one of them and saves via PUT /settings/ai-backends/{n}.
 * Buttons use data-ai-action, never the data-action / data-list-action values
 * of settings.js and lists.js. Icons come from the #lst-i-* sprite. User and
 * backend data is written via textContent.
 */
(function () {
  "use strict";
  const dlg = document.getElementById("dlg-ai");
  const table = document.getElementById("ai-table");
  if (!dlg || !table) return;

  function readJson(id) {
    const node = document.getElementById(id);
    try { return JSON.parse((node && node.textContent) || "null"); } catch (e) { return null; }
  }
  const DATA = readJson("settings-data") || {};
  const T = Object.assign({}, DATA.i18n_settings || {}, DATA.i18n || {});
  const BACKENDS = readJson("ai-backends-data") || [];
  const URL_OK = /^https?:\/\/\S+/i;

  function fmt(text, vars) {
    return String(text).replace(/\{(\w+)\}/g, function (m, k) { return k in vars ? vars[k] : m; });
  }
  function icon(name, extra) {
    return '<svg class="lst-i' + (extra ? " " + extra : "") + '" width="16" height="16" aria-hidden="true" ' +
      'focusable="false"><use href="#lst-i-' + name + '"/></svg>';
  }
  function backendName(id) { return fmt(T.ai_backend_name || "KI-Backend {n}", {n: id}); }
  function backend(id) {
    return BACKENDS.find(function (b) { return b.id === id; }) ||
      {id: id, configured: false, enabled: true, url: "", model: "", slots: 1, key_len: 0};
  }
  function errorLabel() { return T.connection_fail || "Fehler"; }
  function countLabel(n, filtered) {
    return fmt(filtered ? (T.ai_status_vision || "{n} Bildmodelle") : (T.ai_status_models || "{n} Modelle"), {n: n});
  }

  async function api(method, url, body) {
    const opts = {method: method, headers: {}};
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    try {
      const r = await fetch(url, opts);
      return await r.json().catch(function () { return {ok: false, detail: "HTTP " + r.status}; });
    } catch (e) {
      return {ok: false, detail: String(e)};
    }
  }

  // Reload after a change and keep the scroll position (restored by
  // settings.js from "settings_scroll"). With ?edit= in the URL a plain
  // reload would reopen the set editor, so navigate to the section instead.
  function reloadKeep(hash) {
    if (location.search) { location.href = "/settings" + hash; return; }
    try { sessionStorage.setItem("settings_scroll", String(window.scrollY)); } catch (e) { /* private mode */ }
    location.reload();
  }

  // Announces the result of an explicit "test" in the list.
  const live = document.createElement("div");
  live.className = "sr-only";
  live.setAttribute("role", "status");
  live.setAttribute("aria-live", "polite");
  document.body.appendChild(live);

  // ════════════════════════════════════════════════════════════════════
  // List: live status per backend
  // ════════════════════════════════════════════════════════════════════
  const STATUS_ICON = {ok: "wifi", fail: "wifi-off", warn: "triangle", off: "pause", wait: "loader"};

  function renderStatus(cell, kind, label, reason, title) {
    const wrap = document.createElement("div");
    wrap.className = "lst-status-stack";
    const status = document.createElement("span");
    status.className = "lst-status is-" + kind;
    status.innerHTML = icon(STATUS_ICON[kind], kind === "wait" ? "lst-spin" : "");
    status.appendChild(document.createTextNode(label));
    wrap.appendChild(status);
    if (reason) {
      const why = document.createElement("span");
      why.className = "lst-reason";
      why.textContent = reason;
      wrap.appendChild(why);
    }
    wrap.title = title || reason || "";
    cell.replaceChildren(wrap);
    return label + (reason ? ": " + reason : "");
  }

  function applyStatus(row, b, st) {
    const cell = row.querySelector(".ai-state");
    const reach = st.ok ? (T.status_connected || "Verbunden") + " · " + countLabel(st.models, st.filtered)
                        : errorLabel() + ": " + (st.detail || "—");
    if (!b.enabled) return renderStatus(cell, "off", T.status_inactive || "Inaktiv", "", reach);
    if (!st.ok) return renderStatus(cell, "fail", errorLabel(), st.detail || "—");
    if (!b.model) {
      return renderStatus(cell, "warn", T.ai_status_no_model || "Kein Modell",
        T.ai_status_no_model_hint || "Ohne Modell bekommt dieses Backend keine Bilder.");
    }
    if (st.model_available === false) {
      return renderStatus(cell, "warn", T.ai_status_model_missing || "Modell fehlt",
        fmt(T.ai_status_model_missing_hint || "Das Backend bietet „{model}\" nicht an.", {model: b.model}), reach);
    }
    return renderStatus(cell, "ok", reach, "", reach);
  }

  async function loadStatus() {
    const d = await api("GET", "/settings/ai-backends/status");
    const byId = {};
    (d.backends || []).forEach(function (s) { byId[s.id] = s; });
    const said = [];
    table.querySelectorAll("tr[data-ai]").forEach(function (row) {
      const b = backend(parseInt(row.dataset.ai, 10));
      if (!b.configured) return;
      said.push(row.dataset.name + ": " + applyStatus(row, b, byId[b.id] || {ok: false, detail: d.detail || "—"}));
    });
    return said;
  }

  async function testRow(btn) {
    const row = btn.closest("tr");
    renderStatus(row.querySelector(".ai-state"), "wait", T.status_checking || "Prüfe …");
    const said = await loadStatus();
    live.textContent = said.filter(function (s) { return s.indexOf(row.dataset.name) === 0; }).join(" ");
  }

  async function removeBackend(id) {
    if (!confirm(T.ai_delete_confirm || "KI-Backend 2 entfernen? URL, Modell und API-Key werden gelöscht.")) return null;
    const d = await api("DELETE", "/settings/ai-backends/" + id);
    if (d.ok) reloadKeep("#section-ai");
    return d;
  }

  // ════════════════════════════════════════════════════════════════════
  // Popup
  // ════════════════════════════════════════════════════════════════════
  const $ = function (id) { return document.getElementById(id); };
  const f = {
    title: $("dlg-ai-title"), enabled: $("ai-enabled"), url: $("ai-url"), model: $("ai-model"),
    list: $("ai-model-list"), chips: $("ai-model-chips"), status: $("ai-model-status"), warn: $("ai-model-warn"),
    slots: $("ai-slots"), key: $("ai-key"), keyHint: $("ai-key-hint"), test: $("ai-test"), result: $("ai-test-result"),
    del: $("ai-delete"), save: $("ai-save"), error: dlg.querySelector(".modal-error"),
  };
  let editing = null;
  let models = [];
  let modelsUrl = null;
  let modelsReq = 0;
  let urlTimer = null;

  function showError(msg) {
    f.error.querySelector(".modal-error-text").textContent = msg || "";
    f.error.hidden = !msg;
  }
  function setText(node, ok, text, busy) {
    node.className = "status-text" + (ok === true ? " status-ok" : ok === false ? " status-fail" : "");
    node.innerHTML = text ? icon(busy ? "loader" : ok === true ? "check" : ok === false ? "alert" : "cpu", busy ? "lst-spin" : "") : "";
    if (text) node.appendChild(document.createTextNode(text));
  }

  function checkModel() {
    const value = f.model.value.trim();
    f.warn.hidden = !(value && models.length && models.indexOf(value) === -1);
    f.chips.querySelectorAll(".ai-chip").forEach(function (chip) {
      chip.setAttribute("aria-pressed", chip.dataset.model === value ? "true" : "false");
    });
  }

  function renderModels() {
    f.list.replaceChildren();
    f.chips.replaceChildren();
    models.forEach(function (m) {
      const opt = document.createElement("option");
      opt.value = m;
      f.list.appendChild(opt);
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "ai-chip";
      chip.dataset.model = m;
      chip.textContent = m;
      f.chips.appendChild(chip);
    });
    f.chips.hidden = !models.length;
    checkModel();
  }

  // Model ids offered by the typed URL; also the connection test.
  async function fetchModels(force) {
    const url = f.url.value.trim();
    if (!force && url === modelsUrl) return null;
    modelsUrl = url;
    const req = ++modelsReq;
    f.url.removeAttribute("aria-invalid");
    if (!URL_OK.test(url)) {
      models = [];
      renderModels();
      setText(f.status, null, "");
      return {ok: false, detail: url ? (T.ai_url_invalid || "Die URL muss mit http:// oder https:// beginnen.") : ""};
    }
    setText(f.status, null, T.ai_models_loading || "Lade Modelle …", true);
    const d = await api("GET", "/settings/ai-models?url=" + encodeURIComponent(url));
    if (req !== modelsReq) return null;  // a newer URL is already being asked
    models = d.models || [];
    renderModels();
    if (d.ok) {
      setText(f.status, null, fmt(d.filtered ? (T.ai_models_found || "{n} bildfähige Modelle")
        : (T.ai_models_found_any || "{n} Modelle (Bildfähigkeit nicht abfragbar)"), {n: models.length}));
    } else {
      setText(f.status, false, d.detail || errorLabel());
    }
    return d;
  }

  function openEditor(id) {
    const b = backend(id);
    editing = b;
    f.title.textContent = backendName(id);
    f.enabled.checked = b.configured ? !!b.enabled : true;
    f.url.value = b.url || "";
    f.model.value = b.model || "";
    f.slots.value = b.slots || 1;
    f.key.value = "";
    // One dot per stored character, like the old form; the line below says
    // in words whether a key is stored and that empty keeps it.
    f.key.placeholder = "•".repeat(b.key_len || 0);
    f.keyHint.textContent = b.key_len ? fmt(f.keyHint.dataset.stored, {n: b.key_len}) : f.keyHint.dataset.none;
    f.del.hidden = !(id === 2 && b.configured);
    f.save.disabled = false;
    f.url.removeAttribute("aria-invalid");
    f.slots.removeAttribute("aria-invalid");
    showError("");
    setText(f.result, null, "");
    models = [];
    modelsUrl = null;
    renderModels();
    setText(f.status, null, "");
    fetchModels(true);
    dlg.showModal();
    f.url.focus();
  }

  f.url.addEventListener("input", function () {
    clearTimeout(urlTimer);
    urlTimer = setTimeout(function () { fetchModels(false); }, 600);
  });
  f.url.addEventListener("change", function () { clearTimeout(urlTimer); fetchModels(false); });
  f.model.addEventListener("input", checkModel);
  f.chips.addEventListener("click", function (e) {
    const chip = e.target.closest(".ai-chip");
    if (!chip) return;
    f.model.value = chip.dataset.model;
    checkModel();
  });

  f.test.addEventListener("click", async function () {
    clearTimeout(urlTimer);
    setText(f.result, null, T.status_checking || "Prüfe …", true);
    f.test.disabled = true;
    const d = await fetchModels(true);
    f.test.disabled = false;
    if (!d) return;
    if (d.ok) {
      setText(f.result, true, (T.status_connected || "Verbunden") + " · " + countLabel((d.models || []).length, d.filtered));
    } else {
      if (!URL_OK.test(f.url.value.trim())) f.url.setAttribute("aria-invalid", "true");
      setText(f.result, false, d.detail || errorLabel());
    }
  });

  f.save.addEventListener("click", async function () {
    showError("");
    const url = f.url.value.trim();
    if (!URL_OK.test(url)) {
      f.url.setAttribute("aria-invalid", "true");
      showError(T.ai_url_invalid || "Die URL muss mit http:// oder https:// beginnen.");
      f.url.focus();
      return;
    }
    const slots = parseInt(f.slots.value, 10);
    if (!(slots >= 1 && slots <= 16)) {
      f.slots.setAttribute("aria-invalid", "true");
      showError(T.ai_slots_hint || "Gleichzeitige Anfragen an dieses Backend (1–16).");
      f.slots.focus();
      return;
    }
    f.slots.removeAttribute("aria-invalid");
    f.save.disabled = true;
    const d = await api("PUT", "/settings/ai-backends/" + editing.id, {
      enabled: f.enabled.checked, url: url, model: f.model.value.trim(), slots: slots, api_key: f.key.value.trim(),
    });
    if (d.ok) { reloadKeep("#section-ai"); return; }
    f.save.disabled = false;
    showError(d.detail || errorLabel());
  });

  f.del.addEventListener("click", async function () {
    const d = await removeBackend(editing.id);
    if (d && !d.ok) showError(d.detail || errorLabel());
  });

  dlg.addEventListener("click", function (e) {
    if (e.target.closest("[data-close]") && dlg.open) dlg.close();
  });
  dlg.addEventListener("close", function () { clearTimeout(urlTimer); modelsReq++; });

  // ── Buttons in the list ────────────────────────────────────────────
  document.addEventListener("click", async function (e) {
    const btn = e.target.closest("[data-ai-action]");
    if (!btn) return;
    e.preventDefault();
    const id = parseInt(btn.dataset.id, 10);
    const action = btn.dataset.aiAction;
    if (action === "edit") { openEditor(id); return; }
    if (btn.getAttribute("aria-disabled") === "true" || btn.getAttribute("aria-busy") === "true") return;
    btn.setAttribute("aria-busy", "true");
    try {
      if (action === "test") await testRow(btn);
      else if (action === "delete") {
        const d = await removeBackend(id);
        if (d && !d.ok) alert(d.detail || errorLabel());
      }
    } finally { btn.removeAttribute("aria-busy"); }
  });

  loadStatus();
})();
