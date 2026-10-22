/* Settings page: processing sets, Immich accesses, diagnostics tokens.
 *
 * Every list on the page follows one pattern: a compact row per entry, a
 * pencil that opens a popup to edit it, a "+" in the section head that opens
 * a popup to create one. The popups save through their own JSON endpoints;
 * the big "save all" form below only covers the global settings.
 *
 * User data is always written through textContent / value, never innerHTML.
 */
(function () {
  "use strict";

  const DATA = JSON.parse(document.getElementById("settings-data").textContent || "{}");
  // Strings of the "settings" section, overlaid by the "sets" section.
  const T = Object.assign({}, DATA.i18n_settings || {}, DATA.i18n || {});
  const ACCESSES = DATA.accesses || [];

  // ── Small helpers ──────────────────────────────────────────────────
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(function ([k, v]) {
      if (v === undefined || v === null || v === false) return;
      if (k === "text") node.textContent = v;
      else if (k === "class") node.className = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k in node && typeof v !== "string") node[k] = v;
      else node.setAttribute(k, v === true ? "" : v);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function toggle(label, checked, extra) {
    const input = el("input", Object.assign({type: "checkbox"}, extra || {}));
    input.checked = !!checked;
    return el("label", {class: "switch-inline"}, [
      input, el("span", {class: "switch-slider-sm"}), label ? el("span", {text: label}) : null,
    ]);
  }

  function iconButton(label, path, cls) {
    const btn = el("button", {type: "button", class: "icon-btn " + (cls || ""), title: label, "aria-label": label});
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + path + "</svg>";
    return btn;
  }
  const ICON_UP = '<path d="m18 15-6-6-6 6"/>';
  const ICON_DOWN = '<path d="m6 9 6 6 6-6"/>';
  const ICON_REMOVE = '<path d="M18 6 6 18M6 6l12 12"/>';

  async function api(method, url, body, form) {
    const opts = {method: method, headers: {}};
    if (body !== undefined) {
      if (form) {
        opts.headers["Content-Type"] = "application/x-www-form-urlencoded";
        opts.body = new URLSearchParams(body);
      } else {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
      }
    }
    try {
      const r = await fetch(url, opts);
      const d = await r.json().catch(function () { return {ok: false, detail: "HTTP " + r.status}; });
      return d;
    } catch (e) {
      return {ok: false, detail: String(e)};
    }
  }

  function showError(dlg, msg) {
    const box = dlg.querySelector(".modal-error");
    box.textContent = msg || "";
    box.hidden = !msg;
  }

  // Back to the list after a popup saved something. With ?edit= in the URL
  // a plain reload would reopen the editor, so navigate instead.
  function reloadAt(hash) {
    if (location.search) location.href = "/settings" + hash;
    else { location.hash = hash; location.reload(); }
  }

  // Slug of a category label — the same rule the server applies.
  function slugify(label) {
    return (label || "").normalize("NFKD").replace(/[^\x00-\x7f]/g, "")
      .toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  }

  // ── Dialog basics ──────────────────────────────────────────────────
  document.querySelectorAll("dialog.modal").forEach(function (dlg) {
    dlg.addEventListener("click", function (e) {
      if (e.target.closest("[data-close]")) dlg.close();
    });
    // Enter in a text field must not leave the popup by accident.
    dlg.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target.type !== "checkbox") e.preventDefault();
    });
  });

  function fillAccessSelects(scope) {
    scope.querySelectorAll("[data-access-select]").forEach(function (sel) {
      sel.innerHTML = "";
      sel.appendChild(el("option", {value: "", text: T.global_user || "Globaler User"}));
      ACCESSES.filter(function (a) { return !a.global; }).forEach(function (a) {
        sel.appendChild(el("option", {value: String(a.id), text: a.label + (a.active ? "" : " (" + (T.inactive || "inaktiv") + ")")}));
      });
    });
  }

  // ════════════════════════════════════════════════════════════════════
  // Set editor
  // ════════════════════════════════════════════════════════════════════
  const setDlg = document.getElementById("dlg-set");
  const dirRows = document.getElementById("set-directories");
  const ruleRows = document.getElementById("set-rules");
  const catRows = document.getElementById("set-categories");
  let current = null;

  const CONDITIONS = [
    ["filename_contains", T.rule_filename_contains], ["filename_pattern", T.rule_filename_pattern],
    ["exif_expression", T.rule_exif_expression], ["extension", T.rule_extension],
  ];
  const MEDIA = [["", T.rule_media_all], ["image", T.rule_media_image], ["video", T.rule_media_video]];

  function selectTab(key) {
    setDlg.querySelectorAll('[role="tab"]').forEach(function (tab) {
      const on = tab.dataset.tab === key;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      document.getElementById("tab-" + tab.dataset.tab).hidden = !on;
    });
    if (key === "rules") refreshRuleTargets();
  }
  setDlg.querySelector(".tabs").addEventListener("click", function (e) {
    const tab = e.target.closest('[role="tab"]');
    if (tab) selectTab(tab.dataset.tab);
  });
  setDlg.querySelector(".tabs").addEventListener("keydown", function (e) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const tabs = Array.from(setDlg.querySelectorAll('[role="tab"]'));
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    selectTab(next.dataset.tab);
    next.focus();
  });

  function syncVisibility() {
    const mode = setDlg.querySelector('[data-field="schedule_mode"]').value;
    setDlg.querySelectorAll("[data-when-mode]").forEach(function (n) { n.hidden = n.dataset.whenMode !== mode; });
    const target = setDlg.querySelector('[data-field="target_mode"]').value;
    setDlg.querySelectorAll("[data-when-target]").forEach(function (n) { n.hidden = n.dataset.whenTarget !== target; });
  }
  setDlg.querySelector('[data-field="schedule_mode"]').addEventListener("change", syncVisibility);
  setDlg.querySelector('[data-field="target_mode"]').addEventListener("change", syncVisibility);

  // Directories
  function directoryRow(d) {
    const path = el("input", {type: "text", class: "grow", value: d.path || "", placeholder: "/inbox/handy", "aria-label": T.path || "Pfad"});
    const label = el("input", {type: "text", value: d.label || "", placeholder: "Handy", "aria-label": T.label || "Bezeichnung"});
    const remove = iconButton(T.remove || "Entfernen", ICON_REMOVE, "icon-danger");
    const row = el("div", {class: "edit-row", dataset: {id: d.id ? String(d.id) : ""}}, [
      path, label, toggle(T.active, d.active !== false, {"aria-label": T.active}),
      d.id && !d.exists ? el("span", {class: "warn-text", text: T.path_missing || "fehlt"}) : null,
      remove,
    ]);
    remove.addEventListener("click", function () { row.remove(); syncEmpty(); });
    return row;
  }
  function syncEmpty() {
    document.getElementById("set-directories-empty").hidden = dirRows.children.length > 0;
  }

  // Categories
  function categoryRow(c) {
    const row = el("div", {class: "edit-row" + (c.fixed ? " row-fixed" : ""),
                           dataset: {id: c.id ? String(c.id) : "", key: c.key || "", fixed: c.fixed ? "1" : ""}});
    if (c.fixed) {
      row.appendChild(el("span", {class: "fixed-label", text: c.label}));
      row.appendChild(el("span", {class: "muted", text: "→"}));
      row.appendChild(el("code", {class: "grow", text: c.path_template}));
      row.appendChild(el("span", {class: "pill", text: T.fixed || "fest"}));
      return row;
    }
    const label = el("input", {type: "text", value: c.label || "", "aria-label": T.cat_name || "Name"});
    const path = el("input", {type: "text", class: "grow", value: c.path_template || "", placeholder: "ordner/{YYYY}/", "aria-label": T.path || "Pfad"});
    const remove = iconButton(T.remove || "Entfernen", ICON_REMOVE, "icon-danger");
    row.append(label, path, toggle(T.cat_immich_archive, c.immich_archive), remove);
    label.addEventListener("change", function () {
      if (!row.dataset.id) row.dataset.key = slugify(label.value);
      refreshRuleTargets();
    });
    remove.addEventListener("click", function () {
      const key = row.dataset.key;
      const used = Array.from(ruleRows.querySelectorAll("select.rule-target")).some(function (s) { return s.value === key; });
      if (used) { showError(setDlg, T.err_category_in_use || "Kategorie wird noch von einer Regel verwendet."); return; }
      row.remove();
      refreshRuleTargets();
    });
    return row;
  }
  function categoryOptions() {
    return Array.from(catRows.children).map(function (row) {
      const input = row.querySelector("input[type=text]");
      return {key: row.dataset.key, label: input ? input.value : row.querySelector(".fixed-label").textContent};
    }).filter(function (c) { return c.key && c.key !== "error" && c.key !== "duplicate"; });
  }

  // Rules
  function ruleRow(r) {
    const cond = el("select", {"aria-label": T.condition || "Bedingung"});
    CONDITIONS.forEach(function ([v, l]) { cond.appendChild(el("option", {value: v, text: l})); });
    cond.value = r.condition || "filename_contains";
    const value = el("input", {type: "text", class: "grow", value: r.value || "", "aria-label": T.value || "Wert"});
    const target = el("select", {class: "rule-target", "aria-label": T.target || "Ziel", dataset: {value: r.target || ""}});
    const media = el("select", {"aria-label": T.media || "Medientyp"});
    MEDIA.forEach(function ([v, l]) { media.appendChild(el("option", {value: v, text: l})); });
    media.value = r.media_type || "";
    const up = iconButton(T.move_up || "Nach oben", ICON_UP);
    const down = iconButton(T.move_down || "Nach unten", ICON_DOWN);
    const remove = iconButton(T.remove || "Entfernen", ICON_REMOVE, "icon-danger");
    const row = el("div", {class: "edit-row rule-row"}, [
      cond, value, target, media, toggle("", r.active !== false, {"aria-label": T.active}), up, down, remove,
    ]);
    target.addEventListener("change", function () { target.dataset.value = target.value; });
    up.addEventListener("click", function () { if (row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling); });
    down.addEventListener("click", function () { if (row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row); });
    remove.addEventListener("click", function () { row.remove(); });
    return row;
  }
  function refreshRuleTargets() {
    const cats = categoryOptions();
    ruleRows.querySelectorAll("select.rule-target").forEach(function (sel) {
      const keep = sel.dataset.value;
      sel.innerHTML = "";
      sel.appendChild(el("option", {value: "skip", text: "⛔ " + (T.rule_skip || "Überspringen")}));
      cats.forEach(function (c) { sel.appendChild(el("option", {value: c.key, text: c.label || c.key})); });
      sel.value = cats.some(function (c) { return c.key === keep; }) || keep === "skip" ? keep : (cats[0] ? cats[0].key : "skip");
      sel.dataset.value = sel.value;
    });
  }

  setDlg.addEventListener("click", function (e) {
    const add = e.target.closest("[data-add]");
    if (!add) return;
    if (add.dataset.add === "directory") { dirRows.appendChild(directoryRow({active: true})); syncEmpty(); }
    if (add.dataset.add === "rule") { ruleRows.appendChild(ruleRow({})); refreshRuleTargets(); }
    if (add.dataset.add === "category") { catRows.appendChild(categoryRow({})); }
    const last = (add.dataset.add === "directory" ? dirRows : add.dataset.add === "rule" ? ruleRows : catRows).lastElementChild;
    const first = last && last.querySelector("input[type=text], select");
    if (first) first.focus();
  });

  function fill(s) {
    setDlg.querySelector("#dlg-set-title").textContent = (T.edit_title || "Set bearbeiten") + ": " + s.name;
    setDlg.querySelectorAll("[data-field]").forEach(function (input) {
      const v = s[input.dataset.field];
      if (input.type === "checkbox") input.checked = !!v;
      else input.value = v === null || v === undefined ? "" : String(v);
    });
    const days = (s.scheduled_days || "").split(",");
    document.querySelectorAll("#set-days input").forEach(function (cb) { cb.checked = days.indexOf(cb.value) >= 0; });
    document.getElementById("set-last-poll").textContent = s.immich_poll
      ? (T.last_poll || "Letzter Abruf") + ": " + (s.immich_last_poll ? s.immich_last_poll.replace("T", " ").slice(0, 19) : (T.not_yet || "noch nicht"))
      : "";
    document.getElementById("set-base-missing").hidden = !!s.base_path_exists;

    dirRows.innerHTML = "";
    s.directories.forEach(function (d) { dirRows.appendChild(directoryRow(d)); });
    syncEmpty();
    catRows.innerHTML = "";
    s.categories.forEach(function (c) { catRows.appendChild(categoryRow(c)); });
    ruleRows.innerHTML = "";
    s.rules.forEach(function (r) { ruleRows.appendChild(ruleRow(r)); });
    refreshRuleTargets();
    syncVisibility();
  }

  function collect() {
    const out = {};
    setDlg.querySelectorAll("[data-field]").forEach(function (input) {
      const f = input.dataset.field;
      if (input.type === "checkbox") out[f] = input.checked;
      else if (input.type === "number") out[f] = parseInt(input.value, 10) || 0;
      else out[f] = input.value;
    });
    ["immich_user_id", "immich_poll_user_id"].forEach(function (f) { out[f] = out[f] ? parseInt(out[f], 10) : null; });
    out.scheduled_days = Array.from(document.querySelectorAll("#set-days input:checked")).map(function (cb) { return cb.value; }).join(",");
    out.directories = Array.from(dirRows.children).map(function (row) {
      const inputs = row.querySelectorAll("input[type=text]");
      return {id: row.dataset.id ? parseInt(row.dataset.id, 10) : null, path: inputs[0].value.trim(),
              label: inputs[1].value.trim(), active: row.querySelector("input[type=checkbox]").checked};
    });
    out.categories = Array.from(catRows.children).map(function (row) {
      if (row.dataset.fixed) return {id: parseInt(row.dataset.id, 10), label: row.querySelector(".fixed-label").textContent,
                                      path_template: row.querySelector("code").textContent, fixed: true};
      const inputs = row.querySelectorAll("input[type=text]");
      return {id: row.dataset.id ? parseInt(row.dataset.id, 10) : null, key: row.dataset.key || slugify(inputs[0].value),
              label: inputs[0].value.trim(), path_template: inputs[1].value.trim(),
              immich_archive: row.querySelector("input[type=checkbox]").checked};
    });
    out.rules = Array.from(ruleRows.children).map(function (row) {
      const sels = row.querySelectorAll("select");
      return {condition: sels[0].value, value: row.querySelector("input[type=text]").value.trim(),
              target: sels[1].value, media_type: sels[2].value || null,
              active: row.querySelector("input[type=checkbox]").checked};
    });
    return out;
  }

  async function openSet(id) {
    const d = await api("GET", "/settings/sets/" + id);
    if (!d.ok) { alert(d.detail); return; }
    current = d.set;
    fillAccessSelects(setDlg);
    showError(setDlg, "");
    fill(current);
    selectTab("general");
    setDlg.showModal();
    setDlg.querySelector("#set-name").focus();
  }

  setDlg.querySelector(".modal-actions").addEventListener("click", async function (e) {
    const btn = e.target.closest("[data-set-action]");
    if (!btn || !current) return;
    const action = btn.dataset.setAction;
    showError(setDlg, "");
    if (action === "save") {
      btn.disabled = true;
      const d = await api("PUT", "/settings/sets/" + current.id, collect());
      btn.disabled = false;
      if (d.ok) { reloadAt("#section-sets"); }
      else showError(setDlg, d.detail);
    } else if (action === "duplicate") {
      const d = await api("POST", "/settings/sets/" + current.id + "/duplicate");
      if (d.ok) location.href = "/settings?edit=" + d.id + "#section-sets";
      else showError(setDlg, d.detail);
    } else if (action === "delete") {
      if (!confirm((T.delete_confirm || "Set löschen?").replace("{name}", current.name))) return;
      const d = await api("DELETE", "/settings/sets/" + current.id);
      if (d.ok) { reloadAt("#section-sets"); }
      else showError(setDlg, d.detail);
    }
  });

  // ── New set ───────────────────────────────────────────────────────
  const newDlg = document.getElementById("dlg-set-new");
  document.getElementById("new-set-create").addEventListener("click", async function () {
    const name = document.getElementById("new-set-name").value.trim();
    const tpl = document.getElementById("new-set-template").value;
    const d = await api("POST", "/settings/sets", {name: name, copy_from: tpl ? parseInt(tpl, 10) : null});
    if (d.ok) location.href = "/settings?edit=" + d.id + "#section-sets";
    else showError(newDlg, d.detail);
  });

  // ════════════════════════════════════════════════════════════════════
  // Immich accesses
  // ════════════════════════════════════════════════════════════════════
  const accDlg = document.getElementById("dlg-access");
  let accEditing = null;  // null = new, otherwise the access object

  function syncAccessKind() {
    const named = accDlg.querySelector('input[name="access_kind"]:checked').value === "named";
    document.getElementById("access-label-group").hidden = !named;
    document.getElementById("access-active-row").hidden = !named || !accEditing;
  }
  accDlg.querySelectorAll('input[name="access_kind"]').forEach(function (r) { r.addEventListener("change", syncAccessKind); });

  function openAccess(access) {
    accEditing = access;
    showError(accDlg, "");
    const result = document.getElementById("access-test-result");
    result.textContent = ""; result.style.color = "";
    const hasGlobal = ACCESSES.some(function (a) { return a.global; });
    const kind = document.getElementById("access-kind");
    const label = document.getElementById("access-label");
    const key = document.getElementById("access-key");
    key.value = "";
    if (access) {
      kind.hidden = true;
      accDlg.querySelector('input[value="' + (access.global ? "global" : "named") + '"]').checked = true;
      label.value = access.label || "";
      key.placeholder = "•".repeat(access.key_len || 0);
      document.getElementById("access-active").checked = !!access.active;
      document.getElementById("dlg-access-title").textContent = access.global ? (T.global_user || "Globaler User") : access.label;
      const used = document.getElementById("access-used-by");
      used.hidden = !access.used_by.length;
      used.textContent = (T.access_used_by || "Verwendet von") + ": " + access.used_by.join(", ");
      const del = document.getElementById("access-delete");
      del.hidden = false;
      del.disabled = access.used_by.length > 0;
      del.title = access.used_by.length ? (T.access_in_use_hint || "") : "";
    } else {
      kind.hidden = false;
      const g = accDlg.querySelector('input[value="global"]');
      g.disabled = hasGlobal;
      document.getElementById("access-global-taken").hidden = !hasGlobal;
      accDlg.querySelector('input[value="' + (hasGlobal ? "named" : "global") + '"]').checked = true;
      label.value = "";
      key.placeholder = "";
      document.getElementById("dlg-access-title").textContent = T.access_new || "Neuer Zugang";
      document.getElementById("access-used-by").hidden = true;
      document.getElementById("access-delete").hidden = true;
    }
    syncAccessKind();
    accDlg.showModal();
    (access ? key : (accDlg.querySelector('input[name="access_kind"]:checked'))).focus();
  }

  document.getElementById("access-test").addEventListener("click", async function () {
    const out = document.getElementById("access-test-result");
    const typed = document.getElementById("access-key").value.trim();
    out.textContent = "…"; out.style.color = "";
    let d;
    if (accEditing && !accEditing.global) {
      d = await api("POST", "/settings/immich-user/" + accEditing.id + "/test", {iu_api_key: typed}, true);
    } else if (accEditing || typed) {
      d = await api("POST", "/settings/immich/test", {immich_api_key: typed}, true);
    } else {
      d = {ok: false, detail: T.key_required || "Bitte zuerst einen API-Key eingeben."};
    }
    out.textContent = d.detail || "";
    out.style.color = d.ok ? "var(--success)" : "var(--error)";
  });

  document.getElementById("access-save").addEventListener("click", async function () {
    const kind = accDlg.querySelector('input[name="access_kind"]:checked').value;
    const payload = {
      kind: kind,
      label: document.getElementById("access-label").value.trim(),
      api_key: document.getElementById("access-key").value.trim(),
      active: document.getElementById("access-active").checked,
    };
    const d = accEditing
      ? await api("PUT", "/settings/immich-accesses/" + accEditing.id, payload)
      : await api("POST", "/settings/immich-accesses", payload);
    if (d.ok) { reloadAt("#section-immich"); }
    else showError(accDlg, d.detail);
  });

  document.getElementById("access-delete").addEventListener("click", async function () {
    if (!accEditing || !confirm(T.access_delete_confirm || "Zugang löschen?")) return;
    const d = await api("DELETE", "/settings/immich-accesses/" + accEditing.id);
    if (d.ok) { reloadAt("#section-immich"); }
    else showError(accDlg, d.detail);
  });

  async function loadAccessStatus() {
    if (!document.getElementById("access-table")) return;
    const d = await api("GET", "/settings/immich-accesses/status");
    (d.accesses || []).forEach(function (a) {
      const row = document.querySelector('#access-table tr[data-access="' + a.id + '"]');
      if (!row) return;
      row.querySelector(".access-account").textContent = a.ok ? a.detail : "—";
      const dot = row.querySelector(".access-state .dot");
      dot.className = "dot " + (a.ok ? "dot-on" : "dot-fail");
      dot.title = a.ok ? (T.connection_ok || "verbunden") : a.detail;
      row.querySelector(".access-state").appendChild(el("span", {class: a.ok ? "" : "error-text",
        text: " " + (a.ok ? (T.connection_ok || "verbunden") : (T.connection_fail || "Fehler"))}));
    });
  }

  // ════════════════════════════════════════════════════════════════════
  // Diagnostics tokens
  // ════════════════════════════════════════════════════════════════════
  const tokDlg = document.getElementById("dlg-token");
  let tokEditing = null;
  let tokCreated = false;

  function openToken(btn) {
    tokEditing = btn ? {id: btn.dataset.id, label: btn.dataset.label, active: btn.dataset.active === "1"} : null;
    tokCreated = false;
    showError(tokDlg, "");
    const label = document.getElementById("token-label");
    label.value = tokEditing ? tokEditing.label : "";
    label.readOnly = false;
    document.getElementById("token-active").checked = tokEditing ? tokEditing.active : true;
    document.getElementById("token-active-row").hidden = !tokEditing;
    document.getElementById("token-once").hidden = true;
    document.getElementById("token-delete").hidden = !tokEditing;
    const save = document.getElementById("token-save");
    save.hidden = false;
    save.textContent = tokEditing ? (T.save || "Speichern") : (T.generate || "Erzeugen");
    document.getElementById("dlg-token-title").textContent = tokEditing ? tokEditing.label : (T.token_new || "Neuer Token");
    tokDlg.showModal();
    label.focus();
  }
  tokDlg.addEventListener("close", function () { if (tokCreated) reloadAt("#section-diagnostics"); });

  document.getElementById("token-save").addEventListener("click", async function () {
    const label = document.getElementById("token-label").value.trim();
    if (!tokEditing) {
      const d = await api("POST", "/settings/diagnostics/token", {label: label}, true);
      if (!d.ok) { showError(tokDlg, d.detail || "Fehler"); return; }
      tokCreated = true;
      document.getElementById("token-value").value = d.token;
      document.getElementById("token-once").hidden = false;
      document.getElementById("token-label").readOnly = true;
      this.hidden = true;
      document.getElementById("token-value").select();
      return;
    }
    if (label && label !== tokEditing.label) {
      const d = await api("POST", "/settings/diagnostics/token/" + tokEditing.id + "/rename", {label: label}, true);
      if (!d.ok) { showError(tokDlg, T.err_name || "Bitte eine Bezeichnung angeben."); return; }
    }
    if (document.getElementById("token-active").checked !== tokEditing.active) {
      await api("POST", "/settings/diagnostics/token/" + tokEditing.id + "/toggle");
    }
    reloadAt("#section-diagnostics");
  });

  document.getElementById("token-delete").addEventListener("click", async function () {
    if (!tokEditing || !confirm(T.token_delete_confirm || "Token löschen?")) return;
    await api("POST", "/settings/diagnostics/token/" + tokEditing.id + "/delete");
    reloadAt("#section-diagnostics");
  });

  // ── Opening popups from the lists ──────────────────────────────────
  document.addEventListener("click", function (e) {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === "set-edit") openSet(btn.dataset.id);
    else if (action === "set-new") {
      showError(newDlg, "");
      document.getElementById("new-set-name").value = "";
      newDlg.showModal();
      document.getElementById("new-set-name").focus();
    }
    else if (action === "access-new") openAccess(null);
    else if (action === "access-edit") {
      const id = btn.dataset.id === "global" ? "global" : parseInt(btn.dataset.id, 10);
      openAccess(ACCESSES.find(function (a) { return a.id === id; }));
    }
    else if (action === "token-new") openToken(null);
    else if (action === "token-edit") openToken(btn);
  });

  // ════════════════════════════════════════════════════════════════════
  // Global settings form (unchanged behaviour)
  // ════════════════════════════════════════════════════════════════════
  function loadAiModels(modelId, urlId) {
    const urlEl = document.getElementById(urlId);
    const list = document.getElementById(modelId + "_options");
    const hint = document.getElementById(modelId + "_hint");
    if (!list || !hint) return;
    if (!urlEl || !urlEl.value) { list.innerHTML = ""; hint.textContent = ""; return; }
    fetch("/settings/ai-models?url=" + encodeURIComponent(urlEl.value))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        list.innerHTML = "";
        (d.models || []).forEach(function (m) { list.appendChild(el("option", {value: m})); });
        if (d.ok) {
          const tpl = d.filtered ? hint.dataset.found : hint.dataset.any;
          hint.textContent = tpl.replace("{n}", (d.models || []).length);
          hint.style.color = "";
        } else {
          hint.textContent = d.detail;
          hint.style.color = "var(--error)";
        }
      })
      .catch(function (e) { hint.textContent = String(e); hint.style.color = "var(--error)"; });
  }
  [["ai_model", "ai_url"], ["ai2_model", "ai2_url"]].forEach(function (p) {
    loadAiModels(p[0], p[1]);
    const u = document.getElementById(p[1]);
    if (u) u.addEventListener("change", function () { loadAiModels(p[0], p[1]); });
  });

  // Keep the scroll position across the "save all" round trip.
  document.getElementById("settings-form").addEventListener("submit", function () {
    sessionStorage.setItem("settings_scroll", window.scrollY);
  });
  const saved = sessionStorage.getItem("settings_scroll");
  if (saved) { window.scrollTo(0, parseInt(saved, 10)); sessionStorage.removeItem("settings_scroll"); }

  loadAccessStatus();
  if (DATA.edit) openSet(DATA.edit);
})();
