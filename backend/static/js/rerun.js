// "Neu ausführen": run single modules of finished jobs again, with cascade
// (docs/UMBAU_MODULE_AUSFUEHRUNG.md, API section 4, UI section 5).
// Loaded after app.js on every page; each part only starts where its markup
// is on the page:
//   MA.rerun.open({target, modules, label, count, disabled, immich})
//                         the dialog (templates/_rerun.html, rerun_dialog)
//   job detail            menu on the module card, a button per step result,
//                         "wartet" badge (fed by app.js: event ma:job-history)
//   job log               checkbox column and the sticky selection bar
//   Werkzeuge             card #section-rerun (target: a whole set)
//   #rerun-progress       GET /api/reruns/active, then /api/reruns/{id} every second
// The backend sends keys (why, skip, refused, reason, state, error); the texts
// come from i18n (section "rerun"). User data goes in with textContent only.
(function () {
  "use strict";
  const MA = window.MA || (window.MA = {});
  const H = MA.history; // el, iconEl, badge from app.js
  // IA-09 (notification) only mails about failed jobs; like the module strip
  // (job_view.step_visible) the preview leaves it out.
  const ORDER = ["IA-01", "IA-02", "IA-03", "IA-04", "IA-05", "IA-06", "IA-07", "IA-08", "IA-10", "IA-11"];
  const FINISHED = new Set(["done", "cancelled"]);

  // ── Texts ──
  let S = null;
  function strings() {
    if (!S) {
      const blob = document.getElementById("rerun-strings");
      S = blob ? JSON.parse(blob.textContent) : {rerun: {}, steps: {}, lang: "de"};
    }
    return S;
  }
  // Single pass, so a value containing "{n}" stays as it is
  function tr(key, vars) {
    const s = strings().rerun[key];
    if (s == null) return key;
    return s.replace(/\{(\w+)\}/g, (m, k) => (vars && Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m));
  }
  function has(key) { return strings().rerun[key] != null; }
  function stepName(code) { return strings().steps[code] || code; }
  // 1 428 (narrow no-break space) in German, 1,428 in English
  function num(n) {
    const v = Number(n) || 0;
    return String(v).replace(/\B(?=(\d{3})+(?!\d))/g, strings().lang === "en" ? "," : " ");
  }
  function many(base, n, vars) {
    return tr(n === 1 ? base + "_one" : base + "_many", Object.assign({n: num(n)}, vars || {}));
  }
  // "1 Datei" / "12 Dateien"
  function files(n) { return many("sub_files", n); }

  function el(tag, cls, text) { return H.el(tag, cls, text); }
  function iconEl(tag, cls, icon, text, size, spin) { return H.iconEl(tag, cls, icon, text, size, spin); }
  function iconSvg(name, size, spin) {
    return `<svg class="ui-i${spin ? " ui-spin" : ""}" width="${size || 16}" height="${size || 16}" aria-hidden="true" focusable="false"><use href="#ui-i-${name}"/></svg>`;
  }

  // ── API ──
  async function api(method, url, body, signal) {
    const opts = {method, credentials: "same-origin", signal, headers: {"Accept": "application/json"}};
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    let data = {};
    try { data = await resp.json(); } catch (_) { /* no JSON body */ }
    return {ok: resp.ok && data.ok !== false, status: resp.status, data: data || {}};
  }
  function errorText(res) {
    const key = res && res.data && res.data.error;
    if (key && has("err_" + key)) return tr("err_" + key);
    if (res && res.data && res.data.detail) return String(res.data.detail);
    return tr("err_http", {status: res ? res.status : "?"});
  }

  // Module register (GET /api/reruns/modules), fetched once per page
  let registryPromise = null;
  function registry() {
    if (!registryPromise) {
      registryPromise = api("GET", "/api/reruns/modules").then((r) => {
        if (!r.ok || !Array.isArray(r.data.modules)) throw new Error(errorText(r));
        return r.data;
      });
      registryPromise.catch(() => { registryPromise = null; });
    }
    return registryPromise;
  }
  function standalone(reg) { return (reg.modules || []).filter((m) => m.standalone); }

  // ── Toast (DESIGN.md › Toast) ──
  function toast(text, tone) {
    const region = document.getElementById("rerun-toasts");
    if (!region) return;
    const icons = {success: "check-circle", error: "alert-circle", warning: "alert-triangle", info: "info"};
    const t = iconEl("div", "ui-toast is-" + (tone || "info"), icons[tone] || "info", null, 16);
    t.setAttribute("role", tone === "error" ? "alert" : "status");
    t.appendChild(el("span", "ui-toast-text", text));
    region.appendChild(t);
    requestAnimationFrame(() => t.classList.add("is-shown"));
    setTimeout(() => { t.classList.remove("is-shown"); setTimeout(() => t.remove(), 250); }, 5000);
  }

  // Alert box (ui-alert ui-alert-sm) with a title and lines of text
  function alertBox(tone, icon, title, lines, role) {
    const box = iconEl("div", "ui-alert ui-alert-sm is-" + tone, icon, null, 16);
    if (role) box.setAttribute("role", role);
    const body = el("div", "ui-alert-body");
    if (title) body.appendChild(el("p", "ui-alert-title", title));
    for (const line of lines || []) body.appendChild(typeof line === "string" ? el("p", "ui-alert-text", line) : line);
    box.appendChild(body);
    return box;
  }

  // Module checkboxes (dialog and Werkzeuge card): code, name, description
  function moduleChecks(box, reg, name, checked, disabled) {
    box.replaceChildren();
    for (const m of standalone(reg)) {
      const label = el("label", "ui-check rerun-mod");
      const input = el("input");
      input.type = "checkbox";
      input.name = name;
      input.value = m.module;
      input.checked = checked.has(m.module);
      const text = el("span", "rerun-mod-text");
      const title = el("span", "rerun-mod-name");
      title.append(el("span", "rerun-mod-code", m.module), document.createTextNode(stepName(m.module)));
      text.appendChild(title);
      const reason = disabled && disabled[m.module];
      const desc = has("module_desc_" + m.module) ? tr("module_desc_" + m.module) : "";
      if (reason) {
        input.disabled = true;
        input.checked = false;
        label.title = reason;
        text.appendChild(el("span", "rerun-mod-desc is-reason", reason));
      } else if (desc) {
        text.appendChild(el("span", "rerun-mod-desc", desc));
      }
      label.append(input, text);
      box.appendChild(label);
    }
  }

  // ═══ Dialog ═══════════════════════════════════════════════════════════
  const D = {dlg: null, opts: null, reg: null, preview: null, ctrl: null, timer: null, seq: 0, immichSeen: false, busy: false};

  function q(sel) { return D.dlg.querySelector(sel); }
  function checkedModules() {
    return Array.from(D.dlg.querySelectorAll('#rerun-modules input[type="checkbox"]:checked')).map((i) => i.value);
  }
  function radio(name) {
    const r = D.dlg.querySelector(`input[name="${name}"]:checked`);
    return r ? r.value : null;
  }
  function hintsOffered(mods) {
    if (!D.reg) return false;
    const chosen = new Set(mods);
    return (D.reg.modules || []).some((m) => chosen.has(m.module)
      && (m.hint_dependents || []).some((h) => !chosen.has(h)));
  }
  function requestBody(mods) {
    const offered = hintsOffered(mods);
    return {
      modules: mods,
      target: D.opts.target,
      options: {cascade: radio("rerun-cascade") || "auto", hints: offered && q("#rerun-hints").checked,
                immich: radio("rerun-immich") || "api"},
    };
  }

  function setSub(total) {
    const t = D.opts.target;
    const sub = q("#rerun-sub");
    if (t.kind === "set") {
      sub.textContent = total == null ? tr("sub_set_loading", {name: D.opts.label || ""})
        : tr("sub_set", {name: D.opts.label || "", n: num(total)});
    } else if (t.kind === "jobs" && (t.keys || []).length === 1) {
      sub.textContent = D.opts.label || t.keys[0];
    } else {
      const n = total != null ? total : D.opts.count;
      sub.textContent = n == null ? "" : many("sub_files", n);
    }
  }

  function buildDialog() {
    const dlg = document.getElementById("rerun-dialog");
    if (!dlg) return null;
    D.dlg = dlg;
    dlg.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => dlg.close()));
    dlg.addEventListener("close", () => {
      if (D.ctrl) D.ctrl.abort();
      clearTimeout(D.timer);
      D.preview = null;
      const opener = D.opts && D.opts.returnFocus;
      if (opener && document.contains(opener)) opener.focus();
    });
    dlg.addEventListener("change", (e) => {
      const t = e.target;
      if (t.name === "rerun-cascade") q("#rerun-cascade-desc").textContent = tr("cascade_" + t.value + "_desc");
      if (t.name === "rerun-immich") q("#rerun-immich-desc").textContent = tr("immich_" + t.value + "_desc");
      if (t.name === "rerun-modules") syncHints();
      if (t.name && t.name.startsWith("rerun-") || t.id === "rerun-hints") schedulePreview(300);
    });
    q("#rerun-run").addEventListener("click", start);
    return dlg;
  }

  function syncHints() {
    q("#rerun-hints-row").hidden = !hintsOffered(checkedModules());
  }

  async function open(opts) {
    if (!D.dlg && !buildDialog()) return;
    D.opts = Object.assign({returnFocus: document.activeElement}, opts || {});
    D.preview = null;
    D.immichSeen = !!D.opts.immich;
    D.busy = false;
    const t = D.opts.target || {};
    const multi = t.kind === "set" ? "set" : (t.kind === "jobs" && (t.keys || []).length === 1 ? "one" : "many");
    q("#rerun-title").textContent = tr("dialog_title_" + multi);
    setSub(null);
    q("#rerun-error").replaceChildren();
    q("#rerun-summary").textContent = "";
    q("#rerun-hints").checked = false;
    q("#rerun-hints-row").hidden = true;
    q("#rerun-immich-row").hidden = !D.immichSeen;
    setRun(null);
    renderSkeleton();
    if (!D.dlg.open) D.dlg.showModal();

    let reg;
    try {
      reg = D.reg = await registry();
    } catch (e) {
      q("#rerun-modules").replaceChildren(alertBox("error", "alert-circle", tr("preview_failed", {error: e.message}), [], "alert"));
      q("#rerun-preview").replaceChildren();
      return;
    }
    const def = reg.defaults || {};
    for (const [name, value] of [["rerun-cascade", def.cascade || "auto"], ["rerun-immich", def.immich || "api"]]) {
      const r = D.dlg.querySelector(`input[name="${name}"][value="${value}"]`);
      if (r) r.checked = true;
    }
    q("#rerun-hints").checked = !!def.hints;
    q("#rerun-cascade-desc").textContent = tr("cascade_" + (radio("rerun-cascade") || "auto") + "_desc");
    q("#rerun-immich-desc").textContent = tr("immich_" + (radio("rerun-immich") || "api") + "_desc");
    moduleChecks(q("#rerun-modules"), reg, "rerun-modules", new Set(D.opts.modules || []), D.opts.disabled);
    syncHints();
    const first = q('#rerun-modules input:checked:not(:disabled)') || q("#rerun-modules input:not(:disabled)");
    if (first) first.focus();
    schedulePreview(0);
  }

  function setRun(p) {
    const btn = q("#rerun-run");
    const n = p ? p.eligible || 0 : 0;
    btn.disabled = !p || n === 0 || D.busy;
    q("#rerun-run-text").textContent = p ? tr("run_button", {n: num(n)}) : tr("confirm_ok");
  }

  function renderSkeleton() {
    const box = q("#rerun-preview");
    box.setAttribute("aria-busy", "true");
    box.classList.remove("is-loading");
    const steps = el("div", "ui-steps rerun-steps");
    for (let i = 0; i < 4; i++) {
      const row = el("div", "ui-step");
      const head = el("div", "ui-step-head");
      head.appendChild(el("span", "ui-skeleton rerun-skel-line"));
      row.appendChild(head);
      steps.appendChild(row);
    }
    box.replaceChildren(el("p", "ui-sr-only", tr("preview_loading")), steps);
  }

  function schedulePreview(delay) {
    clearTimeout(D.timer);
    if (D.ctrl) D.ctrl.abort();
    const mods = checkedModules();
    q("#rerun-error").replaceChildren();
    if (!mods.length) {
      D.preview = null;
      setRun(null);
      q("#rerun-summary").textContent = "";
      const box = q("#rerun-preview");
      box.removeAttribute("aria-busy");
      box.classList.remove("is-loading");
      box.replaceChildren(iconEl("div", "ui-empty-inline rerun-pick", "info", tr("preview_pick"), 16));
      return;
    }
    // Keep the last preview on screen, dimmed; a skeleton only the first time
    const box = q("#rerun-preview");
    if (D.preview) box.classList.add("is-loading");
    else renderSkeleton();
    box.setAttribute("aria-busy", "true");
    setRun(null);
    D.timer = setTimeout(() => loadPreview(mods), delay);
  }

  async function loadPreview(mods) {
    const ctrl = D.ctrl = new AbortController();
    const seq = ++D.seq;
    const body = Object.assign(requestBody(mods), {detail: "summary"});
    let res;
    try {
      res = await api("POST", "/api/reruns/preview", body, ctrl.signal);
    } catch (e) {
      if (e.name === "AbortError") return;
      res = {ok: false, status: 0, data: {}, network: true};
    }
    if (seq !== D.seq || !D.dlg.open) return; // a newer request is on its way
    const box = q("#rerun-preview");
    box.classList.remove("is-loading");
    box.removeAttribute("aria-busy");
    if (!res.ok) {
      D.preview = null;
      setRun(null);
      q("#rerun-summary").textContent = "";
      box.replaceChildren(alertBox("error", "alert-circle",
        tr("preview_failed", {error: res.network ? tr("err_network") : errorText(res)}), [], "alert"));
      return;
    }
    D.preview = res.data;
    D.previewBody = body;
    renderPreview(res.data);
  }

  function whyText(entry) {
    if (!entry.why) return "";
    const key = "why_" + entry.why;
    if (!has(key)) return entry.why;
    return tr(key, {from: (entry.from || []).join(", ") || "—"});
  }

  const ACTIONS = {
    run: ["is-info", "retry"],
    maybe: ["is-neutral", "circle"],
    keep: ["is-success", "check-circle"],
    skip: ["is-warning", "pause-circle"],
  };

  function renderPreview(p) {
    const total = p.total || 0;
    const eligible = p.eligible || 0;
    setSub(total);
    q("#rerun-summary").textContent = eligible === 0 ? tr("preview_none")
      : eligible === total ? (eligible === 1 ? tr("preview_summary_all_one") : tr("preview_summary_all", {files: files(eligible)}))
        : (eligible === 1 ? tr("preview_summary_one", {total: num(total)})
          : tr("preview_summary", {eligible: num(eligible), total: num(total)}));

    const fx = p.effects || {};
    if ((fx.immich_api || 0) + (fx.immich_replace || 0) > 0) D.immichSeen = true;
    q("#rerun-immich-row").hidden = !D.immichSeen;

    const nodes = [];
    // One row per module, in pipeline order
    const steps = el("div", "ui-steps rerun-steps");
    steps.setAttribute("role", "list");
    const codes = (D.reg && D.reg.modules ? D.reg.modules.map((m) => m.module) : ORDER).filter((c) => c !== "IA-09");
    const per = p.per_module || {};
    for (const code of codes) {
      const entry = per[code] || {action: "keep"};
      const action = ACTIONS[entry.action] ? entry.action : "keep";
      const row = el("div", "ui-step rerun-step");
      row.setAttribute("role", "listitem");
      row.dataset.action = action;
      if (!per[code]) row.classList.add("is-untouched");
      const head = el("div", "ui-step-head");
      const label = tr("action_" + action);
      const status = iconEl("span", "ui-status " + ACTIONS[action][0], ACTIONS[action][1], null, 16);
      status.title = label;
      status.appendChild(el("span", "ui-sr-only", label));
      head.append(status, el("span", "ui-step-code", code), el("span", "ui-step-label", stepName(code)));
      const meta = el("span", "ui-step-meta rerun-step-meta");
      const line = el("span", "rerun-step-line");
      line.appendChild(el("span", "rerun-act is-" + action, label));
      const parts = [];
      if (per[code] && entry.count != null) parts.push(num(entry.count));
      const why = whyText(entry);
      if (why) parts.push(why);
      if (parts.length) line.appendChild(document.createTextNode(" · " + parts.join(" · ")));
      meta.appendChild(line);
      for (const [reason, info] of Object.entries(entry.skip || {})) {
        const text = tr("skip_count", {n: num((info && info.count) || 0),
          reason: has("skip_" + reason) ? tr("skip_" + reason) : reason});
        meta.appendChild(el("span", "rerun-step-skip", text));
      }
      head.appendChild(meta);
      row.appendChild(head);
      steps.appendChild(row);
    }
    nodes.push(steps);

    // What the run does: warnings first (replace, moves, AI, downloads), then the quiet facts
    const warn = risks(fx);
    if (fx.ai_calls_max > 0) warn.push(tr("effects_ai", {n: num(fx.ai_calls_max)}));
    if (fx.downloads > 0) warn.push(tr("effects_downloads", {files: files(fx.downloads)}));
    if (warn.length) {
      const list = el("ul", "rerun-effects");
      for (const w of warn) list.appendChild(el("li", "", w));
      nodes.push(alertBox("warning", "alert-triangle", tr("effects_title"), [list]));
    }
    const facts = [];
    if (fx.file_writes_max > 0) facts.push(tr("effects_writes", {files: files(fx.file_writes_max)}));
    if (fx.immich_api > 0) facts.push(tr("effects_immich_api", {files: files(fx.immich_api)}));
    if (facts.length) nodes.push(iconEl("p", "rerun-facts", "info", facts.join(" · "), 14));

    // Refused jobs, by reason, with examples as links
    const refused = Object.entries(p.refused || {});
    if (refused.length) {
      const n = refused.reduce((s, [, v]) => s + ((v && v.count) || 0), 0);
      const det = el("details", "rerun-refused");
      det.appendChild(el("summary", "", many("refused_title", n)));
      const list = el("ul", "rerun-refused-list");
      for (const [reason, info] of refused) {
        const li = el("li");
        li.append(el("strong", "ui-tabular", num((info && info.count) || 0)),
          document.createTextNode(" " + (has("refused_" + reason) ? tr("refused_" + reason) : reason)));
        const sample = (info && info.sample) || [];
        if (sample.length) {
          const keys = el("span", "rerun-refused-sample");
          for (const key of sample) {
            const a = el("a", "ui-link ui-mono", key);
            a.href = "/logs/job/" + encodeURIComponent(key);
            keys.appendChild(a);
          }
          li.appendChild(keys);
        }
        list.appendChild(li);
      }
      det.appendChild(list);
      nodes.push(alertBox("info", "info", null, [det]));
    }
    q("#rerun-preview").replaceChildren(...nodes);
    setRun(p);
  }

  // The effects that make the confirmation red: new asset IDs, moved files
  function risks(fx) {
    const out = [];
    if (fx.immich_replace > 0) out.push(tr("effects_replace", {files: files(fx.immich_replace)}));
    if (fx.library_moves_max > 0) out.push(tr("effects_moves", {files: files(fx.library_moves_max)}));
    return out;
  }

  async function start() {
    const p = D.preview;
    if (!p || !p.eligible || D.busy) return;
    const mods = p.modules || D.previewBody.modules;
    const body = {modules: D.previewBody.modules, target: D.previewBody.target, options: D.previewBody.options};
    const danger = risks(p.effects || {});
    const vars = {
      modules: mods.map((m) => m + " " + stepName(m)).join(", "),
      files: files(p.eligible),
      cascade: tr("cascade_" + (body.options.cascade || "auto")).toLowerCase(),
    };
    const yes = await MA.confirm({
      title: tr("confirm_title"),
      text: danger.length ? tr("confirm_text_danger", Object.assign({effects: danger.join(" ")}, vars)) : tr("confirm_text", vars),
      ok: tr("confirm_ok"),
      danger: danger.length > 0,
    });
    const btn = q("#rerun-run");
    if (!yes) { btn.focus(); return; }
    D.busy = true;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    let res;
    try {
      res = await api("POST", "/api/reruns", body);
    } catch (_) {
      res = {ok: false, status: 0, data: {}, network: true};
    }
    D.busy = false;
    btn.removeAttribute("aria-busy");
    if (res.ok) {
      const total = res.data.total != null ? res.data.total : p.eligible;
      D.dlg.close();
      toast(total === 1 ? tr("start_ok_one") : tr("start_ok_many", {n: num(total)}), "success");
      document.dispatchEvent(new CustomEvent("ma:rerun-started",
        {detail: {rerun_id: res.data.rerun_id, total, target: body.target}}));
      return;
    }
    setRun(p);
    const text = res.network ? tr("err_network") : errorText(res);
    q("#rerun-error").replaceChildren(alertBox("error", "alert-circle", text, [], "alert"));
    // A bulk run is already going: show it where the page has a progress card
    if (res.data && res.data.error === "rerun_running" && res.data.rerun_id) {
      document.dispatchEvent(new CustomEvent("ma:rerun-started", {detail: {rerun_id: res.data.rerun_id, existing: true}}));
    }
  }

  // ═══ Job detail: menu, step buttons, pending badge ═══════════════════
  function initJobDetail() {
    const box = document.getElementById("job-rerun");
    const header = document.querySelector("[data-page='job-detail']");
    if (!box || !header) return;
    const key = header.dataset.debugKey;
    const wrap = document.getElementById("job-rerun-menu");
    const menu = document.getElementById("job-rerun-items");
    const badge = document.getElementById("job-rerun-pending");
    const isImmich = box.dataset.immich === "1";
    const filename = box.dataset.filename || key;
    let reg = null;
    let state = null; // data.rerun from /api/history/job
    let blocks = [];

    function reasons() {
      const out = {};
      for (const [code, m] of Object.entries((state && state.modules) || {})) {
        if (m && m.possible === false) out[code] = has("skip_" + m.skip) ? tr("skip_" + m.skip) : (m.skip || "");
      }
      return out;
    }
    function openFor(code, opener) {
      open({target: {kind: "jobs", keys: [key]}, modules: code ? [code] : [], label: filename,
            disabled: reasons(), immich: isImmich, returnFocus: opener});
    }

    function buildMenu() {
      menu.replaceChildren();
      for (const m of standalone(reg)) {
        const item = el("button", "ui-menu-item rerun-menu-item");
        item.type = "button";
        item.setAttribute("role", "menuitem");
        item.dataset.module = m.module;
        item.append(el("span", "rerun-menu-code", m.module), el("span", "rerun-menu-name", stepName(m.module)),
          el("span", "rerun-menu-reason"));
        menu.appendChild(item);
      }
      const sep = el("div", "ui-menu-sep");
      sep.setAttribute("role", "separator");
      const more = iconEl("button", "ui-menu-item", "list", tr("menu_more"), 16);
      more.type = "button";
      more.setAttribute("role", "menuitem");
      more.dataset.module = "";
      menu.append(sep, more);
    }
    menu.addEventListener("click", (e) => {
      const item = e.target.closest(".ui-menu-item");
      if (!item || item.disabled) return;
      openFor(item.dataset.module, wrap.querySelector("[aria-haspopup]"));
    });

    function render() {
      if (!reg) return;
      const eligible = !!(state && state.eligible);
      const active = state && state.active;
      wrap.hidden = !eligible;
      badge.hidden = !active;
      if (active) {
        badge.querySelector("[data-f=text]").textContent =
          tr(active.state === "running" ? "badge_running" : "badge_waiting");
      }
      box.hidden = wrap.hidden && badge.hidden;
      const why = reasons();
      menu.querySelectorAll("[data-module]").forEach((item) => {
        const code = item.dataset.module;
        if (!code) return;
        item.disabled = !!why[code];
        item.title = why[code] || "";
        item.querySelector(".rerun-menu-reason").textContent = why[code] || "";
      });
      renderSteps(eligible, why);
    }

    // "Neu ausführen" at the top of each step result of a module that runs alone
    function renderSteps(eligible, why) {
      const codes = new Set(standalone(reg).map((m) => m.module));
      const stale = new Set(blocks.filter((b) => b.stale).map((b) => b.module));
      document.querySelectorAll("#step-results details.ui-step[data-step]").forEach((det) => {
        const code = det.dataset.step;
        const body = det.querySelector(".ui-step-body");
        if (!body) return;
        let row = body.querySelector(":scope > .step-rerun");
        const show = codes.has(code) && eligible;
        if (!show && !stale.has(code)) { if (row) row.remove(); return; }
        if (!row) {
          row = el("div", "step-rerun");
          const mark = H.badge(MA.history.text("stale"), "");
          mark.classList.add("hist-badge-dashed", "step-rerun-stale");
          mark.title = MA.history.text("stale_hint");
          const btn = iconEl("button", "ui-btn ui-btn-ghost ui-btn-sm", "retry", tr("step_button"), 14);
          btn.type = "button";
          btn.dataset.module = code;
          btn.setAttribute("aria-label", tr("step_button_label", {module: code + " " + stepName(code)}));
          btn.addEventListener("click", () => openFor(code, btn));
          row.append(mark, btn);
        }
        body.prepend(row); // always first, above the earlier attempts
        row.querySelector(".step-rerun-stale").hidden = !stale.has(code);
        const btn = row.querySelector("button");
        btn.hidden = !show;
        btn.disabled = !!why[code];
        btn.title = why[code] || "";
      });
    }

    document.addEventListener("ma:job-history", (e) => {
      state = e.detail && e.detail.rerun ? e.detail.rerun : null;
      blocks = (e.detail && e.detail.modules) || [];
      render();
    });
    const steps = document.getElementById("step-results");
    if (steps) steps.addEventListener("ma:steps-rendered", render);
    document.addEventListener("ma:rerun-started", () => document.dispatchEvent(new Event("ma:job-refresh")));
    registry().then((r) => { reg = r; buildMenu(); render(); }).catch(() => { /* menu stays hidden */ });
  }

  // ═══ Job log: selection column and bar ═══════════════════════════════
  function initLogSelection() {
    const bar = document.getElementById("rerun-bar");
    const table = document.querySelector(".logs-table-jobs");
    if (!bar || !table) return;
    const all = document.getElementById("rerun-check-page");
    const meta = bar.querySelector("[data-f=count]");
    const pageBtn = bar.querySelector("[data-f=page]");
    const matchBtn = bar.querySelector("[data-f=matching]");
    const total = Number(bar.dataset.total) || 0;
    let mode = "keys"; // "query": every match of the filter, not only this page

    const boxes = () => Array.from(table.querySelectorAll("tbody input.rerun-row-check"));
    function selected() { return boxes().filter((b) => b.checked); }

    function sync() {
      const list = boxes();
      const chosen = list.filter((b) => b.checked);
      for (const b of list) b.closest("tr").classList.toggle("is-selected", b.checked);
      if (all) {
        all.checked = chosen.length > 0 && chosen.length === list.length;
        all.indeterminate = chosen.length > 0 && chosen.length < list.length;
      }
      if (chosen.length < list.length) mode = "keys";
      const count = mode === "query" ? total : chosen.length;
      bar.hidden = chosen.length === 0;
      meta.textContent = mode === "query" ? tr("selected_all", {n: num(total)})
        : (count === 1 ? tr("selected_one") : tr("selected_many", {n: num(count)}));
      pageBtn.hidden = chosen.length === list.length;
      matchBtn.hidden = mode === "query" || chosen.length < list.length || total <= list.length;
      matchBtn.textContent = tr("select_all_matching", {n: num(total)});
    }
    function setAll(on) {
      for (const b of boxes()) b.checked = on;
      if (!on) mode = "keys";
      sync();
    }

    table.addEventListener("change", (e) => {
      if (e.target === all) { setAll(all.checked); return; }
      if (e.target.classList.contains("rerun-row-check")) { mode = "keys"; sync(); }
    });
    pageBtn.addEventListener("click", () => setAll(true));
    matchBtn.addEventListener("click", () => {
      setAll(true);
      mode = "query";
      sync();
      meta.focus();
    });
    bar.querySelector("[data-f=clear]").addEventListener("click", () => {
      setAll(false);
      const first = boxes()[0];
      if (first) first.focus();
    });
    bar.querySelector("[data-f=run]").addEventListener("click", (e) => {
      let target;
      let label = null;
      let count;
      if (mode === "query") {
        target = {kind: "query"};
        if (bar.dataset.status) target.status = bar.dataset.status;
        if (bar.dataset.q) target.q = bar.dataset.q;
        count = total;
      } else {
        const chosen = selected();
        target = {kind: "jobs", keys: chosen.map((b) => b.value)};
        count = chosen.length;
        if (count === 1) label = chosen[0].dataset.name;
      }
      open({target, modules: [], label, count, returnFocus: e.currentTarget});
    });
    document.addEventListener("ma:rerun-started", (e) => { if (!(e.detail && e.detail.existing)) setAll(false); });
    sync();
  }

  // ═══ Werkzeuge: the card for a whole set ═════════════════════════════
  function initToolsCard() {
    const card = document.getElementById("section-rerun");
    if (!card) return;
    const setSel = document.getElementById("rerun-set");
    const mods = document.getElementById("rerun-tools-modules");
    const btn = document.getElementById("rerun-tools-preview");
    const wanted = new URLSearchParams(location.search).get("rerun_set");

    function chosen() { return Array.from(mods.querySelectorAll("input:checked")).map((i) => i.value); }
    function sync() { btn.disabled = !setSel.value || !chosen().length; }
    card.addEventListener("change", sync);

    fetch("/api/dashboard", {headers: {"Accept": "application/json"}})
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        setSel.replaceChildren();
        for (const s of (d && d.sets) || []) {
          const o = el("option", "", s.name);
          o.value = String(s.id);
          setSel.appendChild(o);
        }
        if (wanted && setSel.querySelector(`option[value="${CSS.escape(wanted)}"]`)) setSel.value = wanted;
        setSel.disabled = !setSel.options.length;
        sync();
        if (wanted && location.hash === "#section-rerun") card.scrollIntoView({block: "start"});
      })
      .catch(() => { /* the select stays empty, the button disabled */ });
    registry().then((reg) => { moduleChecks(mods, reg, "rerun-tools-module", new Set(), null); sync(); })
      .catch((e) => mods.replaceChildren(alertBox("error", "alert-circle", tr("preview_failed", {error: e.message}), [], "alert")));

    btn.addEventListener("click", () => {
      const status = (card.querySelector('input[name="rerun-tools-status"]:checked') || {}).value === "done"
        ? ["done"] : ["done", "review"];
      const from = document.getElementById("rerun-from").value || null;
      const to = document.getElementById("rerun-to").value || null;
      const opt = setSel.options[setSel.selectedIndex];
      open({
        target: {kind: "set", set_id: Number(setSel.value),
                 filter: {status, only_stale: document.getElementById("rerun-only-stale").checked,
                          created_from: from, created_to: to}},
        modules: chosen(), label: opt ? opt.textContent : "", returnFocus: btn,
      });
    });
  }

  // ═══ Progress card ═══════════════════════════════════════════════════
  function initProgress() {
    const card = document.getElementById("rerun-progress");
    if (!card) return;
    const f = (name) => card.querySelector(`[data-f="${name}"]`);
    let id = null;
    let timer = null;
    let lastLive = "";

    function live(text) {
      if (text && text !== lastLive) { lastLive = text; f("live").textContent = text; }
    }
    function moduleNames(list) { return (list || []).map(stepName).join(", "); }

    function render(r) {
      const c = r.counts || {};
      const total = r.total || 0;
      const finished = (c.done || 0) + (c.unchanged || 0) + (c.failed || 0) + (c.refused || 0) + (c.cancelled || 0);
      const pct = total > 0 ? Math.min(100, Math.round((finished / total) * 100)) : 0;
      const over = FINISHED.has(r.state);
      card.hidden = false;
      card.dataset.state = r.state;
      card.classList.toggle("is-paused", !!r.paused && !over);
      f("title").textContent = tr("progress_title", {modules: moduleNames(r.modules)});
      f("counter").textContent = tr("progress_counter", {done: num(finished), total: num(total), pct});
      const track = f("track");
      track.classList.toggle("is-indeterminate", total === 0 && !over);
      track.classList.toggle("is-warning", !!r.paused && !over);
      track.classList.toggle("is-success", over && r.state === "done");
      f("bar").style.width = (over ? 100 : pct) + "%";
      track.setAttribute("aria-valuenow", String(over ? 100 : pct));
      const icon = over ? (r.state === "done" ? "check-circle" : "x-circle") : (r.paused ? "pause-circle" : "loader");
      f("icon").innerHTML = iconSvg(icon, 16, icon === "loader");

      let phase;
      if (over) phase = r.state === "done" ? tr("progress_finished") : tr("state_cancelled");
      else if (r.state === "cancelling") phase = tr("progress_cancelling");
      else if (r.paused) phase = tr("progress_paused");
      else phase = tr("progress_running", {running: num((c.running || 0) + (c.preparing || 0)),
        waiting: num((c.pending || 0) + (c.queued || 0) + (c.deferred || 0))});
      f("phase").textContent = phase;
      f("cancel").hidden = r.state !== "running";
      f("close").hidden = !over;

      const result = f("result");
      const fails = r.failures || [];
      const summary = tr("progress_result", {done: num(c.done || 0), unchanged: num(c.unchanged || 0),
        failed: num(c.failed || 0), refused: num(c.refused || 0)});
      if (over || fails.length) {
        const nodes = [];
        if (over) {
          nodes.push(el("p", "rerun-progress-line", summary));
          if (c.cancelled) nodes.push(el("p", "rerun-progress-line ui-muted", tr("progress_result_cancelled", {cancelled: num(c.cancelled)})));
        }
        if (fails.length) {
          const open = !!result.querySelector("details[open]");
          const det = el("details", "rerun-fails");
          det.open = open;
          // The last 20 failed or refused items (state tells which)
          const anyRefused = fails.some((x) => x.state === "refused");
          det.appendChild(el("summary", "", tr(anyRefused ? "failures_refused_title" : "failures_title", {n: num(fails.length)})));
          const list = el("ul", "rerun-fails-list");
          for (const x of fails) {
            const li = el("li", "rerun-fail");
            const head = el("div", "rerun-fail-head");
            const a = el("a", "ui-link ui-mono", x.debug_key);
            a.href = "/logs/job/" + encodeURIComponent(x.debug_key);
            head.appendChild(a);
            if (x.module) head.appendChild(el("span", "ui-badge", x.module));
            if (anyRefused && x.state && has("state_" + x.state)) {
              head.appendChild(H.badge(tr("state_" + x.state), x.state === "refused" ? "is-info" : "is-error"));
            }
            const kind = x.state === "refused" ? "refused_" : "reason_";
            const reason = x.reason && has(kind + x.reason) ? tr(kind + x.reason) : (x.reason || "");
            if (reason) head.appendChild(el("span", "rerun-fail-reason", reason));
            li.appendChild(head);
            if (x.detail) {
              const d = el("p", "rerun-fail-detail", x.detail);
              d.title = x.detail;
              li.appendChild(d);
            }
            list.appendChild(li);
          }
          det.appendChild(list);
          nodes.push(det);
        }
        result.replaceChildren(...nodes);
        result.hidden = false;
      } else {
        result.hidden = true;
        result.replaceChildren();
      }
      // Read out only when the state changes, not every second
      live(over ? tr("progress_finished") + ": " + summary : (r.paused ? phase : f("title").textContent));
    }

    async function poll() {
      if (id == null) return;
      let res;
      try {
        res = await api("GET", "/api/reruns/" + encodeURIComponent(id));
      } catch (_) {
        schedule(); // network blip: keep polling
        return;
      }
      if (res.ok && res.data && res.data.id != null) {
        render(res.data);
        if (!FINISHED.has(res.data.state)) schedule();
        else id = null;
        return;
      }
      if (res.status === 404) { id = null; return; }
      schedule();
    }
    function schedule() { clearTimeout(timer); timer = setTimeout(poll, 1000); }
    function follow(rerunId) {
      if (rerunId == null) return;
      id = rerunId;
      clearTimeout(timer);
      poll();
    }

    f("cancel").addEventListener("click", async () => {
      const current = id;
      if (current == null) return;
      const yes = await MA.confirm({title: tr("cancel_confirm_title"), text: tr("cancel_confirm_text"),
        ok: tr("cancel_confirm_ok"), danger: true});
      if (!yes) return;
      const btn = f("cancel");
      btn.disabled = true;
      let res;
      try { res = await api("POST", "/api/reruns/" + encodeURIComponent(current) + "/cancel", {}); }
      catch (_) { res = {ok: false, status: 0, data: {}, network: true}; }
      btn.disabled = false;
      if (res.ok) toast(tr("cancelled"), "info");
      else toast(res.network ? tr("err_network") : errorText(res), "error");
      follow(current);
    });
    f("close").addEventListener("click", () => { card.hidden = true; });
    document.addEventListener("ma:rerun-started", (e) => follow(e.detail && e.detail.rerun_id));

    api("GET", "/api/reruns/active").then((res) => {
      if (res.ok && res.data && res.data.rerun && id == null) {
        render(res.data.rerun);
        if (!FINISHED.has(res.data.rerun.state)) follow(res.data.rerun.id);
      }
    }).catch(() => { /* no card until a run starts */ });
  }

  initJobDetail();
  initLogSelection();
  initToolsCard();
  initProgress();

  MA.rerun = {open};
})();
