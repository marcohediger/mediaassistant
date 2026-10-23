/* Settings page: processing sets, Immich accesses, diagnostics tokens.
 *
 * Every list on the page follows one pattern: a compact row per entry, a
 * pencil that opens a popup to edit it, a "+" that opens a popup to create
 * one. The popups save through their own JSON endpoints; the big "save all"
 * form below only covers the global settings.
 *
 * The set editor keeps its lists (inputs, rules, categories) as plain state,
 * renders read-only rows from it and edits single entries in a second popup
 * on top. Nothing is sent before the set's own "Speichern".
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

  // Static icon markup only — never user data.
  const ICONS = {
    up: '<path d="m18 15-6-6-6 6"/>',
    down: '<path d="m6 9 6 6 6-6"/>',
    edit: '<path d="M21.17 6.81a1 1 0 0 0-3.99-3.99L3.84 16.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.35-1.32a2 2 0 0 0 .83-.5z"/><path d="m15 5 4 4"/>',
    trash: '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M10 11v6M14 11v6"/>',
    alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4M12 17h.01"/>',
    ok: '<circle cx="12" cy="12" r="10"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
    fail: '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/>',
    folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    image: '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.09-3.09a2 2 0 0 0-2.82 0L6 21"/>',
    ban: '<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>',
    archive: '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8M10 12h4"/>',
  };
  function icon(name, size) {
    const s = size || 16;
    const wrap = document.createElement("span");
    wrap.innerHTML = '<svg class="ico" width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true" focusable="false">' + ICONS[name] + "</svg>";
    return wrap.firstChild;
  }

  function iconButton(label, name, cls) {
    const btn = el("button", {type: "button", class: "icon-btn " + (cls || ""), title: label, "aria-label": label});
    btn.appendChild(icon(name, 16));
    return btn;
  }

  // One cell of a grid table; data-label is shown as a caption on phones.
  function cell(cls, label, children) {
    return el("div", {class: "gt-cell " + cls, dataset: label ? {label: label} : undefined}, children);
  }
  function text(value, cls) { return el("span", {class: cls || "cell-text", text: value, title: value || null}); }
  function stateEl(on) {
    return el("span", {class: "state " + (on ? "state-on" : "state-off")}, [
      el("span", {class: "state-dot", "aria-hidden": "true"}),
      el("span", {class: "state-text", text: on ? (T.active || "Aktiv") : (T.inactive || "inaktiv")}),
    ]);
  }

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
    box.querySelector(".modal-error-text").textContent = msg || "";
    box.hidden = !msg;
  }

  function setStatus(node, ok, msg) {
    node.textContent = "";
    node.className = "status-text" + (ok === true ? " status-ok" : ok === false ? " status-fail" : "");
    if (ok === true || ok === false) node.appendChild(icon(ok ? "ok" : "fail", 15));
    if (msg) node.appendChild(el("span", {text: msg}));
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

  function accessLabel(id) {
    if (!id) return T.global_user || "Globaler User";
    const a = ACCESSES.find(function (x) { return x.id === id; });
    return a ? a.label + (a.active ? "" : " (" + (T.inactive || "inaktiv") + ")") : "#" + id;
  }
  function fillAccessSelect(sel) {
    sel.innerHTML = "";
    sel.appendChild(el("option", {value: "", text: T.global_user || "Globaler User"}));
    ACCESSES.filter(function (a) { return !a.global; }).forEach(function (a) {
      sel.appendChild(el("option", {value: String(a.id), text: accessLabel(a.id)}));
    });
  }

  // ── Dialog basics ──────────────────────────────────────────────────
  // Each popup remembers what had the focus before it opened (popups stack).
  const openers = new WeakMap();
  function openDialog(dlg, focusTarget) {
    openers.set(dlg, document.activeElement);
    dlg.showModal();
    if (focusTarget) focusTarget.focus();
  }
  // Popups that may hold unsaved input register a guard here.
  const closeGuards = new Map();

  document.querySelectorAll("dialog.modal").forEach(function (dlg) {
    dlg.addEventListener("click", function (e) {
      if (!e.target.closest("[data-close]")) return;
      const guard = closeGuards.get(dlg);
      if (guard && !guard()) return;
      dlg.close();
    });
    dlg.addEventListener("cancel", function (e) {
      const guard = closeGuards.get(dlg);
      if (guard && !guard()) e.preventDefault();
    });
    dlg.addEventListener("close", function () {
      const o = openers.get(dlg);
      openers.delete(dlg);
      if (o && document.contains(o) && !o.disabled) o.focus();
    });
    // Enter in a text field must not leave the popup by accident.
    dlg.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target.type !== "checkbox") e.preventDefault();
    });
    // A field marked invalid after a failed save is cleared on the next edit.
    dlg.addEventListener("input", function (e) {
      if (e.target.getAttribute && e.target.getAttribute("aria-invalid")) e.target.removeAttribute("aria-invalid");
    });
  });

  // ════════════════════════════════════════════════════════════════════
  // Set editor
  // ════════════════════════════════════════════════════════════════════
  const setDlg = document.getElementById("dlg-set");
  const inputRows = document.getElementById("set-inputs");
  const ruleRows = document.getElementById("set-rules");
  const catRows = document.getElementById("set-categories");
  const scroller = setDlg.querySelector(".editor-scroll");
  const tablist = setDlg.querySelector('[role="tablist"]');
  let current = null;
  let baseline = "";

  // Editor state. The lists are rendered from it and collect() reads it.
  let dirs = [];          // {id, path, label, active, exists, origPath}
  let immich = null;      // the set's Immich input {active, user_id} or null
  let pollUserId = null;  // access of the Immich input, kept when it is removed
  let rules = [];         // {condition, value, target, media_type, active}
  let cats = [];          // {id, key, label, path_template, fixed, immich_archive}

  const COND_LABEL = {
    filename_contains: T.rule_filename_contains || "Dateiname enthält",
    filename_pattern: T.cond_pattern || "Dateiname-Muster",
    exif_expression: T.cond_exif || "EXIF-Ausdruck",
    extension: T.rule_extension || "Dateiendung",
  };
  const MEDIA_LABEL = {"": T.rule_media_all || "Alle", image: T.rule_media_image || "Bilder", video: T.rule_media_video || "Videos"};
  const FIXED_HINT = T.fixed_hint || "Feste Kategorie — nicht änderbar";

  // ── Section navigation ──
  function tabs() { return Array.from(tablist.querySelectorAll('[role="tab"]')); }

  function selectTab(key) {
    tabs().forEach(function (tab) {
      const on = tab.dataset.tab === key;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      document.getElementById("tab-" + tab.dataset.tab).hidden = !on;
      if (on) tab.scrollIntoView({block: "nearest", inline: "nearest"});
    });
    scroller.scrollTop = 0;
    closeMenu(false);
  }
  tablist.addEventListener("click", function (e) {
    const tab = e.target.closest('[role="tab"]');
    if (tab) selectTab(tab.dataset.tab);
  });
  tablist.addEventListener("keydown", function (e) {
    const list = tabs();
    const i = list.indexOf(document.activeElement);
    if (i < 0) return;
    let next = null;
    if (e.key === "ArrowDown" || e.key === "ArrowRight") next = list[(i + 1) % list.length];
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") next = list[(i + list.length - 1) % list.length];
    else if (e.key === "Home") next = list[0];
    else if (e.key === "End") next = list[list.length - 1];
    if (!next) return;
    e.preventDefault();
    selectTab(next.dataset.tab);
    next.focus();
  });
  // The section list turns into a horizontal strip on narrow screens.
  const narrow = window.matchMedia("(max-width: 760px)");
  function syncOrientation() { tablist.setAttribute("aria-orientation", narrow.matches ? "horizontal" : "vertical"); }
  syncOrientation();
  if (narrow.addEventListener) narrow.addEventListener("change", syncOrientation);

  // ── Dependent visibility ──
  function radioValue(field) {
    const r = setDlg.querySelector('[data-radio-field="' + field + '"]:checked');
    return r ? r.value : "";
  }
  function fieldInput(name) { return setDlg.querySelector('[data-field="' + name + '"]'); }

  function syncVisibility() {
    const mode = radioValue("schedule_mode");
    setDlg.querySelectorAll("[data-when-mode]").forEach(function (n) {
      n.hidden = n.dataset.whenMode.split(" ").indexOf(mode) < 0;
    });
    const target = radioValue("target_mode");
    setDlg.querySelectorAll("[data-when-target]").forEach(function (n) { n.hidden = n.dataset.whenTarget !== target; });
    // Ziel-Ablage shows what takes effect in the chosen mode; the state keeps everything.
    $("tab-target").classList.toggle("is-immich", target === "immich");
    renderCats();
    // The folder example greys out the tag modes not chosen.
    const tagMode = radioValue("folder_tag_mode");
    setDlg.querySelectorAll("[data-when-tag-mode]").forEach(function (n) {
      n.classList.toggle("is-off", n.dataset.whenTagMode !== tagMode);
    });
    // With the old word tags the album keeps its old name too.
    setDlg.querySelectorAll("[data-album-for]").forEach(function (n) {
      n.hidden = (n.dataset.albumFor === "legacy") !== (tagMode === "legacy");
    });
    // Controls that only matter while a switch is on are greyed out.
    setDlg.querySelectorAll("[data-depends]").forEach(function (n) {
      const on = fieldInput(n.dataset.depends).checked;
      n.classList.toggle("is-off", !on);
      n.querySelectorAll("input, select").forEach(function (c) { c.disabled = !on; });
    });
  }

  // Times are plain text fields (no native clock); "2200", "7" or "7.30"
  // become HH:MM when the field is left.
  function normalizeTime(raw) {
    const s = raw.trim().replace(/[.,h ]/g, ":");
    let x = /^(\d{1,2}):(\d{1,2})$/.exec(s) || /^(\d{1,2})()$/.exec(s) || /^(\d{1,2})(\d{2})$/.exec(s);
    if (!x) return null;
    const h = parseInt(x[1], 10), m = parseInt(x[2] || "0", 10);
    if (h > 23 || m > 59) return null;
    return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
  }
  setDlg.querySelectorAll("[data-time]").forEach(function (input) {
    input.addEventListener("change", function () {
      const v = normalizeTime(input.value);
      if (v) { input.value = v; input.removeAttribute("aria-invalid"); }
      else input.setAttribute("aria-invalid", "true");
    });
  });

  // ── Focus helpers for re-rendered lists ──
  function focusRow(container, selector, fallbackBtn) {
    const target = selector && container.querySelector(selector);
    const btn = target || fallbackBtn;
    if (btn && !btn.disabled) btn.focus();
    else if (fallbackBtn) fallbackBtn.focus();
  }
  function addButtonOf(container) { return container.closest(".gtable").querySelector(".btn-add"); }

  // ── Inputs: directories + the single Immich input ──
  function lastPollText() {
    const unchanged = current && current.immich_poll && immich && immich.active && immich.user_id === current.immich_poll_user_id;
    const when = unchanged && current.immich_last_poll ? current.immich_last_poll.replace("T", " ").slice(0, 19) : (T.not_yet || "noch nicht");
    return (T.last_poll || "Letzter Abruf") + ": " + when;
  }
  function typeChip(iconName, label) {
    return el("span", {class: "type-chip"}, [icon(iconName, 15), el("span", {text: label})]);
  }
  function renderInputs() {
    inputRows.textContent = "";
    dirs.forEach(function (d, i) {
      const edit = iconButton(T.edit || "Bearbeiten", "edit", "row-edit");
      const del = iconButton(T.remove || "Entfernen", "trash", "icon-danger row-del");
      const missing = d.id && !d.exists && d.path === d.origPath
        ? el("span", {class: "field-note warn"}, [icon("alert", 13), el("span", {text: T.path_missing || "Pfad nicht gefunden"})])
        : null;
      inputRows.appendChild(el("div", {class: "gt-row" + (d.active ? "" : " row-inactive"), role: "listitem",
                                       dataset: {kind: "dir", index: String(i)}}, [
        cell("c-type", T.col_type || "Typ", [typeChip("folder", T.input_dir || "Verzeichnis")]),
        cell("c-source", T.col_source || "Quelle", [text(d.path, "cell-code"), missing]),
        cell("c-label", T.label, [text(d.label)]),
        cell("c-status", T.col_status, [stateEl(d.active)]),
        cell("c-actions end", null, [edit, del]),
      ]));
      edit.addEventListener("click", function () { openItem("directory", i); });
      del.addEventListener("click", function () {
        dirs.splice(i, 1);
        renderInputs(); refreshState();
        focusRow(inputRows, '[data-kind="dir"][data-index="' + Math.min(i, dirs.length - 1) + '"] .row-del', document.getElementById("input-add"));
      });
    });
    if (immich) {
      const edit = iconButton(T.edit || "Bearbeiten", "edit", "row-edit");
      const del = iconButton(T.remove || "Entfernen", "trash", "icon-danger row-del");
      inputRows.appendChild(el("div", {class: "gt-row" + (immich.active ? "" : " row-inactive"), role: "listitem",
                                       dataset: {kind: "immich"}}, [
        cell("c-type", T.col_type || "Typ", [typeChip("image", T.input_immich || "Immich")]),
        cell("c-source", T.col_source || "Quelle", [text(accessLabel(immich.user_id))]),
        cell("c-label", T.label, [text(T.immich_poll_short || "Immich Auto-Tagging"), text(lastPollText(), "cell-sub")]),
        cell("c-status", T.col_status, [stateEl(immich.active)]),
        cell("c-actions end", null, [edit, del]),
      ]));
      edit.addEventListener("click", function () { openItem("immich", null); });
      del.addEventListener("click", function () {
        pollUserId = immich.user_id;
        immich = null;
        renderInputs(); refreshState();
        focusRow(inputRows, ".gt-row:last-child .row-del", document.getElementById("input-add"));
      });
    }
    renderCats();
  }

  // "+ Eingang hinzufügen" opens a small menu: directory or Immich.
  const addBtn = document.getElementById("input-add");
  const addMenu = document.getElementById("input-add-menu");
  function menuItems() { return Array.from(addMenu.querySelectorAll('[role="menuitem"]:not(:disabled)')); }
  function openMenu() {
    const taken = !!immich;
    const im = addMenu.querySelector('[data-add-input="immich"]');
    im.disabled = taken;
    im.querySelector("[data-when-free]").hidden = taken;
    im.querySelector("[data-when-taken]").hidden = !taken;
    addMenu.hidden = false;
    addBtn.setAttribute("aria-expanded", "true");
    addMenu.scrollIntoView({block: "nearest"});
    menuItems()[0].focus();
  }
  function closeMenu(focusButton) {
    if (addMenu.hidden) return;
    addMenu.hidden = true;
    addBtn.setAttribute("aria-expanded", "false");
    if (focusButton) addBtn.focus();
  }
  addBtn.addEventListener("click", function () { if (addMenu.hidden) openMenu(); else closeMenu(true); });
  addMenu.addEventListener("keydown", function (e) {
    const items = menuItems();
    const i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
    else if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i + items.length - 1) % items.length].focus(); }
    else if (e.key === "Tab") closeMenu(false);
  });
  addMenu.addEventListener("click", function (e) {
    const item = e.target.closest("[data-add-input]");
    if (!item || item.disabled) return;
    closeMenu(true);
    openItem(item.dataset.addInput === "immich" ? "immich" : "directory", null);
  });
  setDlg.addEventListener("click", function (e) {
    if (!addMenu.hidden && !e.target.closest(".add-menu")) closeMenu(false);
  });

  // ── Rules ──
  function ruleTargetOptions() {
    return cats.filter(function (c) { return c.key && c.key !== "error" && c.key !== "duplicate"; });
  }
  function targetEl(key) {
    if (key === "skip") return el("span", {class: "target-skip"}, [icon("ban", 14), el("span", {text: T.rule_skip_short || "Überspringen"})]);
    const c = cats.find(function (x) { return x.key === key; });
    return c ? text(c.label) : el("span", {class: "field-note warn"}, [icon("alert", 13), el("span", {text: key})]);
  }
  function renderRules() {
    ruleRows.textContent = "";
    rules.forEach(function (r, i) {
      const up = iconButton(T.move_up || "Nach oben", "up", "move-up");
      const down = iconButton(T.move_down || "Nach unten", "down", "move-down");
      const edit = iconButton(T.edit || "Bearbeiten", "edit", "row-edit");
      const del = iconButton(T.remove || "Entfernen", "trash", "icon-danger row-del");
      up.disabled = i === 0;
      down.disabled = i === rules.length - 1;
      ruleRows.appendChild(el("div", {class: "gt-row" + (r.active ? "" : " row-inactive"), role: "listitem", dataset: {index: String(i)}}, [
        cell("c-pos center", null, [el("span", {class: "pos", text: String(i + 1)})]),
        cell("c-cond", T.condition, [text(COND_LABEL[r.condition] || r.condition)]),
        cell("c-value", T.value, [text(r.value, "cell-code")]),
        cell("c-target", T.target, [targetEl(r.target)]),
        cell("c-media", T.media, [text(MEDIA_LABEL[r.media_type || ""])]),
        cell("c-status", T.col_status, [stateEl(r.active)]),
        cell("c-actions end", null, [up, down, edit, del]),
      ]));
      up.addEventListener("click", function () { moveRule(i, -1); });
      down.addEventListener("click", function () { moveRule(i, 1); });
      edit.addEventListener("click", function () { openItem("rule", i); });
      del.addEventListener("click", function () {
        rules.splice(i, 1);
        renderRules(); refreshState();
        focusRow(ruleRows, '[data-index="' + Math.min(i, rules.length - 1) + '"] .row-del', addButtonOf(ruleRows));
      });
    });
  }
  function moveRule(i, dir) {
    const j = i + dir;
    if (j < 0 || j >= rules.length) return;
    const tmp = rules[i]; rules[i] = rules[j]; rules[j] = tmp;
    renderRules(); refreshState();
    // Keep the focus on the moved rule's button, or its twin at the edge.
    const row = '[data-index="' + j + '"] ';
    focusRow(ruleRows, row + (dir < 0 ? ".move-up:not(:disabled)" : ".move-down:not(:disabled)"),
             ruleRows.querySelector(row + (dir < 0 ? ".move-down" : ".move-up")));
  }

  // ── Categories ──
  // Paths only matter for the library; the archive flag only where files
  // end up in Immich (upload, or polled assets that stay there).
  function showCatPath() { return radioValue("target_mode") !== "immich"; }
  function archiveMatters() { return radioValue("target_mode") === "immich" || (!!immich && immich.active); }
  function renderCats() {
    const showPath = showCatPath(), showArchive = archiveMatters();
    const table = $("set-categories-table");
    table.classList.toggle("no-path", !showPath);
    table.classList.toggle("no-archive", !showArchive);
    catRows.textContent = "";
    cats.forEach(function (c, i) {
      // Fixed categories are folders below the base path — not shown for Immich.
      if (c.fixed && !showPath) return;
      const edit = iconButton(T.edit || "Bearbeiten", "edit", "row-edit");
      const del = iconButton(T.remove || "Entfernen", "trash", "icon-danger row-del");
      const used = rules.filter(function (r) { return r.target === c.key; }).length;
      if (c.fixed) {
        edit.disabled = del.disabled = true;
        edit.title = del.title = FIXED_HINT;
      } else if (used) {
        del.title = T.err_category_in_use || "Diese Kategorie wird noch von einer Regel verwendet.";
        del.classList.add("is-blocked");
      }
      catRows.appendChild(el("div", {class: "gt-row" + (c.fixed ? " row-fixed" : ""), role: "listitem", dataset: {index: String(i)}}, [
        cell("c-name", T.cat_name, [el("span", {class: "name-line"}, [
          text(c.label, "cell-strong"), c.fixed ? el("span", {class: "badge", text: T.fixed || "fest", title: FIXED_HINT}) : null])]),
        showPath ? cell("c-path", T.path, [text(c.path_template, "cell-code muted-code")]) : null,
        showArchive ? cell("c-archive", T.col_archive || "Immich-Archiv", [c.immich_archive
          ? el("span", {class: "badge badge-accent", title: T.archive_title || "Im Zeitstrahl ausgeblendet"},
               [icon("archive", 12), el("span", {text: T.archive_short || "Archiv"})])
          : el("span", {class: "muted", text: "—"})]) : null,
        cell("c-actions end", null, [edit, del]),
      ]));
      edit.addEventListener("click", function () { openItem("category", i); });
      del.addEventListener("click", function () {
        if (rules.some(function (r) { return r.target === c.key; })) {
          showError(setDlg, "„" + c.label + "“: " + (T.err_category_in_use || "Diese Kategorie wird noch von einer Regel verwendet."));
          return;
        }
        cats.splice(i, 1);
        renderCats(); refreshState();
        focusRow(catRows, '[data-index="' + Math.min(i, cats.length - 1) + '"] .row-del', addButtonOf(catRows));
      });
    });
  }

  setDlg.addEventListener("click", function (e) {
    const add = e.target.closest("[data-add]");
    if (!add) return;
    openItem(add.dataset.add === "rule" ? "rule" : "category", null);
  });

  // ════════════════════════════════════════════════════════════════════
  // Item popup (directory / Immich / rule / category) on top of the editor
  // ════════════════════════════════════════════════════════════════════
  const itemDlg = document.getElementById("dlg-item");
  const $ = function (id) { return document.getElementById(id); };
  let item = null;  // {kind, index} — index null = new entry

  function setRadio(name, value) {
    itemDlg.querySelectorAll('input[name="' + name + '"]').forEach(function (r) { r.checked = r.value === value; });
  }
  function getRadio(name) {
    const r = itemDlg.querySelector('input[name="' + name + '"]:checked');
    return r ? r.value : "";
  }
  function syncRuleHint() {
    const cond = getRadio("item_rule_cond");
    itemDlg.querySelectorAll("[data-hint]").forEach(function (n) { n.hidden = n.dataset.hint !== cond; });
  }
  itemDlg.querySelectorAll('input[name="item_rule_cond"]').forEach(function (r) { r.addEventListener("change", syncRuleHint); });

  // Placeholder chips insert their text at the cursor of the path field.
  $("item-cat-placeholders").addEventListener("click", function (e) {
    const chip = e.target.closest("[data-insert]");
    if (!chip) return;
    const input = $("item-cat-path");
    const start = input.selectionStart === null ? input.value.length : input.selectionStart;
    const end = input.selectionEnd === null ? start : input.selectionEnd;
    input.value = input.value.slice(0, start) + chip.dataset.insert + input.value.slice(end);
    input.focus();
    input.setSelectionRange(start + chip.dataset.insert.length, start + chip.dataset.insert.length);
  });

  function openItem(kind, index) {
    item = {kind: kind, index: index};
    itemDlg.querySelectorAll(".item-form").forEach(function (f) { f.hidden = f.dataset.form !== kind; });
    itemDlg.querySelectorAll("[aria-invalid]").forEach(function (n) { n.removeAttribute("aria-invalid"); });
    showError(itemDlg, "");
    const isNew = index === null;
    let title, focus;
    if (kind === "directory") {
      const d = isNew ? {path: "", label: "", active: true} : dirs[index];
      $("item-dir-path").value = d.path;
      $("item-dir-label").value = d.label;
      $("item-dir-active").checked = !!d.active;
      $("item-dir-missing").hidden = !(d.id && !d.exists && d.path === d.origPath);
      title = isNew ? (T.input_dir_new || "Neues Verzeichnis") : (T.input_dir_edit || "Verzeichnis bearbeiten");
      focus = $("item-dir-path");
    } else if (kind === "immich") {
      const m = immich || {active: true, user_id: pollUserId};
      const sel = $("item-immich-access");
      fillAccessSelect(sel);
      sel.value = m.user_id ? String(m.user_id) : "";
      $("item-immich-active").checked = !!m.active;
      $("item-immich-poll").textContent = immich ? lastPollText() : (T.last_poll || "Letzter Abruf") + ": " + (T.not_yet || "noch nicht");
      title = T.input_immich_title || "Immich-Eingang";
      focus = sel;
    } else if (kind === "rule") {
      const opts = ruleTargetOptions();
      const r = isNew ? {condition: "filename_contains", value: "", target: opts[0] ? opts[0].key : "skip", media_type: null, active: true}
                      : rules[index];
      setRadio("item_rule_cond", r.condition);
      $("item-rule-value").value = r.value;
      const sel = $("item-rule-target");
      sel.innerHTML = "";
      sel.appendChild(el("option", {value: "skip", text: T.rule_skip || "Überspringen"}));
      opts.forEach(function (c) { sel.appendChild(el("option", {value: c.key, text: c.label || c.key})); });
      if (!Array.from(sel.options).some(function (o) { return o.value === r.target; })) {
        sel.appendChild(el("option", {value: r.target, text: r.target}));
      }
      sel.value = r.target;
      setRadio("item_rule_media", r.media_type || "");
      $("item-rule-active").checked = !!r.active;
      syncRuleHint();
      title = isNew ? (T.rule_new || "Neue Regel") : (T.rule_edit || "Regel bearbeiten") + " #" + (index + 1);
      focus = isNew ? itemDlg.querySelector('input[name="item_rule_cond"]:checked') : $("item-rule-value");
    } else {
      const c = isNew ? {label: "", path_template: "", immich_archive: false} : cats[index];
      $("item-cat-path-field").hidden = !showCatPath();
      $("item-cat-archive-wrap").hidden = !archiveMatters();
      $("item-cat-label").value = c.label;
      $("item-cat-path").value = c.path_template;
      $("item-cat-archive").checked = !!c.immich_archive;
      title = isNew ? (T.cat_new || "Neue Kategorie") : (T.cat_edit || "Kategorie bearbeiten");
      focus = $("item-cat-label");
    }
    $("dlg-item-title").textContent = title;
    openDialog(itemDlg, focus);
  }

  function invalid(input, msg) {
    input.setAttribute("aria-invalid", "true");
    input.focus();
    return msg;
  }
  // Python's re accepts a few things JavaScript does not; translate those
  // before judging, the server has the last word anyway.
  function regexProblem(pattern) {
    const translated = pattern.replace(/\(\?P</g, "(?<").replace(/\(\?P=(\w+)\)/g, "\\k<$1>")
      .replace(/^\(\?[aiLmsux]+\)/, "").replace(/\\Z/g, "$").replace(/\\A/g, "^");
    let first = null;
    for (const p of [pattern, translated]) {
      try { new RegExp(p); return null; } catch (e) { if (!first) first = String(e.message || e); }
    }
    return first.split(": ").pop();
  }

  // Validates the open form and writes it into the editor state.
  // Returns an error message, or null when applied.
  function applyItem() {
    const k = item.kind, i = item.index, isNew = i === null;
    if (k === "directory") {
      const pathEl = $("item-dir-path"), labelEl = $("item-dir-label");
      const path = pathEl.value.trim(), label = labelEl.value.trim();
      const msg = T.err_directory || "Jedes Verzeichnis braucht einen absoluten Pfad und eine Bezeichnung.";
      if (!path.startsWith("/")) return invalid(pathEl, msg);
      if (!label) return invalid(labelEl, msg);
      const norm = function (p) { return p.replace(/\/+$/, "") || "/"; };
      if (dirs.some(function (d, j) { return j !== i && norm(d.path) === norm(path); })) {
        return invalid(pathEl, (T.err_dir_twice || "Das Verzeichnis {path} steht zweimal in der Liste.").replace("{path}", norm(path)));
      }
      const inside = function (a, b) { a = norm(a) + "/"; b = norm(b) + "/"; return a !== b && (a.startsWith(b) || b.startsWith(a)); };
      const outer = dirs.find(function (d, j) { return j !== i && inside(path, d.path); });
      if (outer) {
        return invalid(pathEl, (T.err_dir_nested_self || "{path} liegt in {other} oder umgekehrt — der äussere Ordner deckt den inneren bereits ab.")
          .replace("{path}", norm(path)).replace("{other}", norm(outer.path)));
      }
      const d = isNew ? {id: null, exists: true, origPath: null} : dirs[i];
      Object.assign(d, {path: path, label: label, active: $("item-dir-active").checked});
      if (isNew) dirs.push(d);
      renderInputs();
      return null;
    }
    if (k === "immich") {
      const v = $("item-immich-access").value;
      immich = {active: $("item-immich-active").checked, user_id: v ? parseInt(v, 10) : null};
      pollUserId = immich.user_id;
      renderInputs();
      return null;
    }
    if (k === "rule") {
      const valueEl = $("item-rule-value");
      const cond = getRadio("item_rule_cond");
      const value = valueEl.value.trim();
      if (!value) return invalid(valueEl, T.err_rule_value || "Bitte einen Wert angeben.");
      if (cond === "filename_pattern") {
        const problem = regexProblem(value);
        if (problem) {
          return invalid(valueEl, (T.err_rule_regex || "Ungültiges Muster in der Regel „{value}“.").replace("{value}", value) + " — " + problem);
        }
      }
      const r = {condition: cond, value: value, target: $("item-rule-target").value,
                 media_type: getRadio("item_rule_media") || null, active: $("item-rule-active").checked};
      if (isNew) rules.push(r); else rules[i] = r;
      renderRules(); renderCats();
      return null;
    }
    // category
    const labelEl = $("item-cat-label"), pathEl = $("item-cat-path");
    const label = labelEl.value.trim();
    let path = pathEl.value.trim();
    const msg = T.err_category || "Jede Kategorie braucht einen Namen und einen Pfad.";
    if (!label) return invalid(labelEl, msg);
    if (showCatPath() && !path) return invalid(pathEl, msg);
    const c = isNew ? {id: null, key: "", fixed: false, path_template: "", immich_archive: false} : cats[i];
    if (!c.id) {
      // Unsaved categories get their key from the label, like on the server.
      const key = slugify(label);
      if (!key || key === "skip" || cats.some(function (o, j) { return j !== i && o.key === key; })) {
        return invalid(labelEl, (T.err_category_key || "Die Kategorie „{label}“ kollidiert mit einer bestehenden.").replace("{label}", label));
      }
      if (c.key && c.key !== key) rules.forEach(function (r) { if (r.target === c.key) r.target = key; });
      c.key = key;
    }
    // Hidden fields keep their value; the server still wants a path, so a
    // category created for Immich gets one named after its key.
    if (!showCatPath()) path = c.path_template || c.key + "/";
    const archive = archiveMatters() ? $("item-cat-archive").checked : !!c.immich_archive;
    Object.assign(c, {label: label, path_template: path, immich_archive: archive});
    if (isNew) cats.push(c);
    renderCats(); renderRules();
    return null;
  }

  $("item-apply").addEventListener("click", function () {
    showError(itemDlg, "");
    const k = item.kind, i = item.index;
    const problem = applyItem();
    if (problem) { showError(itemDlg, problem); return; }
    // The row behind the pencil was re-rendered: focus its replacement.
    openers.delete(itemDlg);
    itemDlg.close();
    refreshState();
    const index = i === null ? (k === "directory" ? dirs.length - 1 : k === "rule" ? rules.length - 1 : cats.length - 1) : i;
    if (k === "immich") focusRow(inputRows, '[data-kind="immich"] .row-edit', addBtn);
    else if (k === "directory") focusRow(inputRows, '[data-kind="dir"][data-index="' + index + '"] .row-edit', addBtn);
    else {
      const box = k === "rule" ? ruleRows : catRows;
      const row = box.querySelector('[data-index="' + index + '"]');
      if (row) row.scrollIntoView({block: "nearest"});
      focusRow(box, '[data-index="' + index + '"] .row-edit', addButtonOf(box));
    }
  });
  // Enter in a text field of the item popup applies it.
  itemDlg.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target.type === "text") $("item-apply").click();
  });

  // ── Live state: badges, section counters, empty rows, dirty flag ──
  function setMeta(key, value, dot) {
    const node = setDlg.querySelector('[data-meta="' + key + '"]');
    node.textContent = value || "";
    node.className = "editor-tab-meta" + (dot ? " meta-dot " + dot : "");
  }
  function refreshState() {
    $("set-inputs-empty").hidden = dirs.length > 0 || !!immich;
    $("set-rules-empty").hidden = rules.length > 0;
    setMeta("inputs", String(dirs.length + (immich ? 1 : 0)));
    setMeta("rules", String(rules.length));
    setMeta("folder", "", radioValue("folder_tag_mode") !== "none" || fieldInput("folder_albums").checked ? "on" : "off");
    setMeta("ocr", "", fieldInput("ocr_enabled").checked ? "on" : "off");

    const badges = $("set-badges");
    badges.textContent = "";
    const active = fieldInput("active").checked;
    badges.appendChild(el("span", {class: "badge " + (active ? "badge-ok" : "badge-off")},
      [el("span", {class: "badge-dot", "aria-hidden": "true"}), el("span", {text: active ? (T.active || "Aktiv") : (T.inactive || "inaktiv")})]));
    if (fieldInput("dry_run").checked) {
      badges.appendChild(el("span", {class: "badge badge-warn"},
        [el("span", {class: "badge-dot", "aria-hidden": "true"}), el("span", {text: T.dry_run_short || "Probelauf"})]));
    }
    $("set-dirty").hidden = !isDirty();
  }
  function isDirty() { return !!current && JSON.stringify(collect()) !== baseline; }

  setDlg.addEventListener("input", refreshState);
  setDlg.addEventListener("change", function () { syncVisibility(); refreshState(); });
  closeGuards.set(setDlg, function () {
    return !isDirty() || confirm(T.discard_confirm || "Nicht gespeicherte Änderungen verwerfen?");
  });

  function fill(s) {
    $("dlg-set-name").textContent = s.name;
    setDlg.querySelectorAll("[data-field]").forEach(function (input) {
      const v = s[input.dataset.field];
      input.removeAttribute("aria-invalid");
      if (input.type === "checkbox") input.checked = !!v;
      else input.value = v === null || v === undefined ? "" : String(v);
    });
    setDlg.querySelectorAll("[data-radio-field]").forEach(function (r) {
      r.checked = r.value === s[r.dataset.radioField];
    });
    const days = (s.scheduled_days || "").split(",");
    document.querySelectorAll("#set-days input").forEach(function (cb) { cb.checked = days.indexOf(cb.value) >= 0; });
    $("set-base-missing").hidden = !!s.base_path_exists;

    dirs = s.directories.map(function (d) {
      return {id: d.id, path: d.path, label: d.label, active: !!d.active, exists: !!d.exists, origPath: d.path};
    });
    immich = s.immich_input ? {active: !!s.immich_poll, user_id: s.immich_poll_user_id} : null;
    pollUserId = s.immich_poll_user_id;
    rules = s.rules.map(function (r) {
      return {condition: r.condition, value: r.value, target: r.target, media_type: r.media_type || null, active: !!r.active};
    });
    cats = s.categories.map(function (c) { return Object.assign({}, c); });
    renderInputs(); renderRules(); renderCats();
    syncVisibility();
  }

  // The PUT body — the same shape the server has always received.
  function collect() {
    const out = {};
    setDlg.querySelectorAll("[data-field]").forEach(function (input) {
      const f = input.dataset.field;
      if (input.type === "checkbox") out[f] = input.checked;
      else if (input.type === "number") out[f] = parseInt(input.value, 10) || 0;
      else out[f] = input.value;
    });
    setDlg.querySelectorAll("[data-radio-field]:checked").forEach(function (r) { out[r.dataset.radioField] = r.value; });
    out.immich_user_id = out.immich_user_id ? parseInt(out.immich_user_id, 10) : null;
    out.immich_input = !!immich;
    out.immich_poll = !!immich && immich.active;
    out.immich_poll_user_id = immich ? immich.user_id : pollUserId;
    out.scheduled_days = Array.from(document.querySelectorAll("#set-days input:checked")).map(function (cb) { return cb.value; }).join(",");
    out.directories = dirs.map(function (d) {
      return {id: d.id || null, path: d.path, label: d.label, active: d.active};
    });
    out.categories = cats.map(function (c) {
      if (c.fixed) return {id: c.id, label: c.label, path_template: c.path_template, fixed: true};
      return {id: c.id || null, key: c.key || slugify(c.label), label: c.label, path_template: c.path_template,
              immich_archive: !!c.immich_archive};
    });
    out.rules = rules.map(function (r) {
      return {condition: r.condition, value: r.value, target: r.target, media_type: r.media_type || null, active: r.active};
    });
    return out;
  }

  // ── Server errors: find the section and, where possible, the field ──
  const ERROR_SECTIONS = {
    err_name: "general",
    err_interval: "schedule", err_time: "schedule", err_days: "schedule", err_schedule: "schedule",
    err_directory: "inputs", err_dir_twice: "inputs", err_dir_taken: "inputs",
    err_dir_nested: "inputs", err_dir_nested_self: "inputs",
    err_rule: "rules", err_rule_target: "rules", err_rule_regex: "rules",
    err_base_path: "target", err_category: "target", err_category_key: "target", err_category_fixed: "target",
  };
  function matchError(detail) {
    for (const key of Object.keys(ERROR_SECTIONS)) {
      if (!T[key]) continue;
      const parts = T[key].split(/\{[a-z_]+\}/).map(function (p) { return p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); });
      const m = new RegExp("^" + parts.join("(.*)") + "$").exec(detail || "");
      if (m) return {key: key, args: m.slice(1)};
    }
    return null;
  }
  const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
  function lastIndex(list, test) { for (let j = list.length - 1; j >= 0; j--) if (test(list[j])) return j; return -1; }
  // Either {field} for a form control or {box, row} for a list entry.
  function errorTarget(hit) {
    const a = hit.args[0] || "";
    const norm = function (p) { return (p || "").trim().replace(/\/+$/, "") || "/"; };
    let i;
    switch (hit.key) {
      case "err_name": return {field: $("set-name")};
      case "err_interval": return {field: $("set-interval")};
      case "err_time":
        return {field: ["set-wstart", "set-wend", "set-stime"].map($).find(function (n) { return !TIME_RE.test(n.value); })};
      case "err_days": return {field: document.querySelector("#set-days input")};
      case "err_base_path": return {field: $("set-base")};
      case "err_directory":
        i = dirs.findIndex(function (d) { return !d.path.trim().startsWith("/") || !d.label.trim(); });
        return {box: inputRows, row: '[data-kind="dir"][data-index="' + i + '"]'};
      case "err_dir_twice": case "err_dir_taken": case "err_dir_nested": case "err_dir_nested_self":
        i = lastIndex(dirs, function (d) { return norm(d.path) === a; });
        return {box: inputRows, row: '[data-kind="dir"][data-index="' + i + '"]'};
      case "err_rule":
        return {box: ruleRows, row: '[data-index="' + rules.findIndex(function (r) { return !r.value.trim(); }) + '"]'};
      case "err_rule_target": case "err_rule_regex":
        return {box: ruleRows, row: '[data-index="' + rules.findIndex(function (r) { return r.value === a; }) + '"]'};
      case "err_category":
        i = cats.findIndex(function (c) { return !c.fixed && (!c.label.trim() || !c.path_template.trim()); });
        return {box: catRows, row: '[data-index="' + i + '"]'};
      case "err_category_key":
        return {box: catRows, row: '[data-index="' + cats.findIndex(function (c) { return c.label === a; }) + '"]'};
    }
    return {};
  }
  function revealError(detail) {
    showError(setDlg, detail);
    const hit = matchError(detail);
    if (!hit) return;
    selectTab(ERROR_SECTIONS[hit.key]);
    const t = errorTarget(hit);
    if (t.field && !t.field.disabled) {
      t.field.setAttribute("aria-invalid", "true");
      t.field.scrollIntoView({block: "center"});
      t.field.focus();
    } else if (t.box) {
      const row = t.box.querySelector(t.row);
      if (!row) return;
      row.classList.add("is-invalid");
      row.scrollIntoView({block: "center"});
      const edit = row.querySelector(".row-edit");
      if (edit) edit.focus();
    }
  }

  async function openSet(id) {
    const d = await api("GET", "/settings/sets/" + id);
    if (!d.ok) { alert(d.detail); return; }
    current = d.set;
    setDlg.querySelectorAll("[data-access-select]").forEach(fillAccessSelect);
    showError(setDlg, "");
    fill(current);
    baseline = JSON.stringify(collect());
    refreshState();
    selectTab("general");
    openDialog(setDlg, $("set-name"));
  }

  setDlg.querySelector(".modal-foot").addEventListener("click", async function (e) {
    const btn = e.target.closest("[data-set-action]");
    if (!btn || !current) return;
    const action = btn.dataset.setAction;
    showError(setDlg, "");
    if (action === "save") {
      btn.disabled = true;
      const d = await api("PUT", "/settings/sets/" + current.id, collect());
      btn.disabled = false;
      if (d.ok) { baseline = JSON.stringify(collect()); reloadAt("#section-sets"); }
      else revealError(d.detail);
    } else if (action === "duplicate") {
      if (isDirty() && !confirm(T.discard_confirm || "Nicht gespeicherte Änderungen verwerfen?")) return;
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
    document.getElementById("access-active-wrap").hidden = !named || !accEditing;
  }
  accDlg.querySelectorAll('input[name="access_kind"]').forEach(function (r) { r.addEventListener("change", syncAccessKind); });

  function openAccess(access) {
    accEditing = access;
    showError(accDlg, "");
    setStatus(document.getElementById("access-test-result"), null, "");
    const hasGlobal = ACCESSES.some(function (a) { return a.global; });
    const kind = document.getElementById("access-kind");
    const label = document.getElementById("access-label");
    const key = document.getElementById("access-key");
    key.value = "";
    // Whether a key is stored, and how long it is — the key itself never comes back.
    const keyLen = access ? access.key_len || 0 : 0;
    key.placeholder = "•".repeat(keyLen);
    const keyHint = document.getElementById("access-key-hint");
    if (keyLen) setStatus(keyHint, true, (T.access_key_stored || "Schlüssel gespeichert ({n} Zeichen). Leer lassen, um ihn zu behalten.").replace("{n}", keyLen));
    else { keyHint.className = "field-hint"; keyHint.textContent = T.access_key_where || "Den API-Key findest du in Immich unter Kontoeinstellungen → API-Schlüssel."; }
    if (access) {
      kind.hidden = true;
      accDlg.querySelector('input[value="' + (access.global ? "global" : "named") + '"]').checked = true;
      label.value = access.label || "";
      document.getElementById("access-active").checked = !!access.active;
      document.getElementById("dlg-access-title").textContent = access.global ? (T.global_user || "Globaler User") : access.label;
      const used = document.getElementById("access-used-by");
      used.hidden = !access.used_by.length;
      used.textContent = (T.access_used_by || "Verwendet von") + ": " + access.used_by.join(", ");
      const del = document.getElementById("access-delete");
      del.hidden = false;
      del.disabled = access.used_by.length > 0;
      del.title = access.used_by.length ? (T.access_in_use_hint || "") : (T.delete || "Löschen");
    } else {
      kind.hidden = false;
      const g = accDlg.querySelector('input[value="global"]');
      g.disabled = hasGlobal;
      document.getElementById("access-global-taken").hidden = !hasGlobal;
      accDlg.querySelector('input[value="' + (hasGlobal ? "named" : "global") + '"]').checked = true;
      label.value = "";
      document.getElementById("dlg-access-title").textContent = T.access_new || "Neuer Zugang";
      document.getElementById("access-used-by").hidden = true;
      document.getElementById("access-delete").hidden = true;
    }
    syncAccessKind();
    openDialog(accDlg, access ? key : accDlg.querySelector('input[name="access_kind"]:checked'));
  }

  document.getElementById("access-test").addEventListener("click", async function () {
    const out = document.getElementById("access-test-result");
    const typed = document.getElementById("access-key").value.trim();
    setStatus(out, null, "…");
    this.disabled = true;
    let d;
    if (accEditing && !accEditing.global) {
      d = await api("POST", "/settings/immich-user/" + accEditing.id + "/test", {iu_api_key: typed}, true);
    } else if (accEditing || typed) {
      d = await api("POST", "/settings/immich/test", {immich_api_key: typed}, true);
    } else {
      d = {ok: false, detail: T.key_required || "Bitte zuerst einen API-Key eingeben."};
    }
    this.disabled = false;
    setStatus(out, !!d.ok, d.detail || "");
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
    document.getElementById("token-active-wrap").hidden = !tokEditing;
    document.getElementById("token-create-hint").hidden = !!tokEditing;
    document.getElementById("token-once").hidden = true;
    document.getElementById("token-delete").hidden = !tokEditing;
    const save = document.getElementById("token-save");
    save.hidden = false;
    save.textContent = tokEditing ? (T.save || "Speichern") : (T.generate || "Erzeugen");
    document.getElementById("dlg-token-title").textContent = tokEditing ? tokEditing.label : (T.token_new || "Neuer Token");
    openDialog(tokDlg, label);
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
      document.getElementById("token-create-hint").hidden = true;
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

  document.getElementById("token-copy").addEventListener("click", async function () {
    const input = document.getElementById("token-value");
    const btnLabel = this.querySelector("span");
    let ok = false;
    try { await navigator.clipboard.writeText(input.value); ok = true; } catch (e) {
      // No clipboard API without HTTPS — fall back to the selection.
      input.select();
      try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
    }
    if (!ok) { input.select(); return; }
    btnLabel.textContent = T.copied || "Kopiert";
    setTimeout(function () { btnLabel.textContent = T.copy || "Kopieren"; }, 1800);
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
      openDialog(newDlg, document.getElementById("new-set-name"));
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

  if (DATA.edit) openSet(DATA.edit);
})();
