/* Settings page — the lists (processing sets, Immich accesses, diagnostics
 * tokens): quick actions in the "Aktionen" column and the live connection
 * status of the Immich accesses.
 *
 * Opening the edit/create popups is handled by settings.js through the
 * data-action attributes; this file only reacts to data-list-action and must
 * not reuse those attribute values. Icons come from the #lst-i-* sprite in
 * templates/settings/_sets_list.html. User data is written via textContent.
 */
(function () {
  "use strict";
  const dataEl = document.getElementById("settings-data");
  const DATA = JSON.parse((dataEl && dataEl.textContent) || "{}");
  const T = Object.assign({}, DATA.i18n_settings || {}, DATA.i18n || {});

  function icon(name, extra) {
    return '<svg class="lst-i' + (extra ? " " + extra : "") + '" width="16" height="16" aria-hidden="true" ' +
      'focusable="false"><use href="#lst-i-' + name + '"/></svg>';
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
      return await r.json().catch(function () { return {ok: false, detail: "HTTP " + r.status}; });
    } catch (e) {
      return {ok: false, detail: String(e)};
    }
  }

  // ── Feedback: a small toast that is also a live region ─────────────
  // Created up front: a live region added together with its text is often
  // not announced.
  const toast = document.createElement("div");
  toast.className = "lst-toast";
  toast.setAttribute("role", "status");
  toast.setAttribute("aria-live", "polite");
  toast.addEventListener("click", function () { toast.classList.remove("is-shown"); });
  document.body.appendChild(toast);
  let toastTimer = null;
  function say(message, kind) {
    toast.className = "lst-toast is-" + (kind || "info");
    toast.innerHTML = icon(kind === "ok" ? "check" : "alert");
    const text = document.createElement("span");
    text.textContent = message || "";
    toast.appendChild(text);
    // Next frame, so the transition runs and screen readers get the change.
    requestAnimationFrame(function () { toast.classList.add("is-shown"); });
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.classList.remove("is-shown"); }, kind === "fail" ? 9000 : 5000);
  }
  function errorLabel() { return T.connection_fail || "Fehler"; }

  // Reload after a change and keep the scroll position. settings.js restores
  // "settings_scroll" on load; with ?edit= in the URL a plain reload would
  // reopen the editor, so navigate to the section instead.
  function reloadKeep(hash) {
    if (location.search) { location.href = "/settings" + hash; return; }
    try { sessionStorage.setItem("settings_scroll", String(window.scrollY)); } catch (e) { /* private mode */ }
    location.reload();
  }

  // ── Immich accesses: live status ───────────────────────────────────
  function renderStatus(cell, kind, label, title) {
    const span = document.createElement("span");
    span.className = "lst-status is-" + kind;
    const name = {ok: "wifi", fail: "wifi-off", off: "wifi-off", wait: "loader"}[kind];
    span.innerHTML = icon(name, kind === "wait" ? "lst-spin" : "");
    span.appendChild(document.createTextNode(label));
    if (title) span.title = title;
    cell.replaceChildren(span);
  }

  function applyAccessResult(row, ok, detail) {
    const account = row.querySelector(".access-account");
    const state = row.querySelector(".access-state");
    const value = document.createElement("span");
    if (ok) {
      value.textContent = detail || "";
    } else {
      // No account known: show why instead, the status column says "Fehler".
      value.textContent = detail || "—";
    }
    value.title = detail || "";
    account.replaceChildren(value);
    if (row.dataset.active === "0") renderStatus(state, "off", T.status_inactive || "Inaktiv", detail);
    else if (ok) renderStatus(state, "ok", T.status_connected || "Verbunden", detail);
    else renderStatus(state, "fail", errorLabel(), detail);
  }

  async function loadAccessStatus() {
    const table = document.getElementById("access-table");
    if (!table) return;
    const d = await api("GET", "/settings/immich-accesses/status");
    const done = new Set();
    (d.accesses || []).forEach(function (a) {
      const row = table.querySelector('tr[data-access="' + CSS.escape(String(a.id)) + '"]');
      if (!row) return;
      done.add(row);
      applyAccessResult(row, a.ok, a.detail);
    });
    // Rows the status call did not answer for (e.g. request failed).
    table.querySelectorAll("tr[data-access]").forEach(function (row) {
      if (!done.has(row)) applyAccessResult(row, false, d.detail || "—");
    });
  }

  async function testAccess(btn) {
    const row = btn.closest("tr");
    const id = btn.dataset.id;
    renderStatus(row.querySelector(".access-state"), "wait", T.status_checking || "Prüfe …");
    const d = id === "global"
      ? await api("POST", "/settings/immich/test", {immich_api_key: ""}, true)
      : await api("POST", "/settings/immich-user/" + encodeURIComponent(id) + "/test", {iu_api_key: ""}, true);
    applyAccessResult(row, d.ok, d.detail);
    say((d.ok ? (T.status_connected || "Verbunden") : errorLabel()) + (d.detail ? ": " + d.detail : ""), d.ok ? "ok" : "fail");
  }

  // ── Quick actions ──────────────────────────────────────────────────
  const ACTIONS = {
    "set-duplicate": async function (btn) {
      const d = await api("POST", "/settings/sets/" + btn.dataset.id + "/duplicate");
      // Same flow as "Duplizieren" in the editor: open the copy right away.
      if (d.ok) location.href = "/settings?edit=" + d.id + "#section-sets";
      else say(d.detail || errorLabel(), "fail");
    },
    "set-delete": async function (btn) {
      const text = (T.delete_confirm || "Set „{name}\" löschen?").replace("{name}", btn.dataset.name || "");
      if (!confirm(text)) return;
      const d = await api("DELETE", "/settings/sets/" + btn.dataset.id);
      if (d.ok) reloadKeep("#section-sets");
      else say(d.detail || errorLabel(), "fail");
    },
    "access-test": testAccess,
    "access-delete": async function (btn) {
      if (!confirm(T.access_delete_confirm || "Diesen Immich-Zugang löschen?")) return;
      const d = await api("DELETE", "/settings/immich-accesses/" + encodeURIComponent(btn.dataset.id));
      if (d.ok) reloadKeep("#section-immich");
      else say(d.detail || errorLabel(), "fail");
    },
    "token-toggle": async function (btn) {
      const d = await api("POST", "/settings/diagnostics/token/" + encodeURIComponent(btn.dataset.id) + "/toggle");
      if (d.ok) reloadKeep("#section-diagnostics");
      else say(d.detail || errorLabel(), "fail");
    },
    "token-delete": async function (btn) {
      if (!confirm(T.token_delete_confirm || "Diesen Token löschen?")) return;
      const d = await api("POST", "/settings/diagnostics/token/" + encodeURIComponent(btn.dataset.id) + "/delete");
      if (d.ok) reloadKeep("#section-diagnostics");
      else say(d.detail || errorLabel(), "fail");
    },
  };

  document.addEventListener("click", async function (e) {
    const btn = e.target.closest("[data-list-action]");
    if (!btn) return;
    e.preventDefault();
    const run = ACTIONS[btn.dataset.listAction];
    if (!run) return;
    // Blocked (e.g. access still in use): explain instead of doing nothing.
    if (btn.getAttribute("aria-disabled") === "true") { say(btn.dataset.blocked || btn.title, "fail"); return; }
    if (btn.getAttribute("aria-busy") === "true") return;
    btn.setAttribute("aria-busy", "true");
    try { await run(btn); } finally { btn.removeAttribute("aria-busy"); }
  });

  loadAccessStatus();
})();
