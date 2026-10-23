// MediaAssistant Frontend JS — Auto-Refresh

(function () {
  const POLL_INTERVAL = 5000;

  // ── Dashboard auto-refresh ──
  // The rows rendered here mirror the macros set_row / job_row in
  // templates/dashboard.html: change both together.
  function initDashboard() {
    const statsGrid = document.getElementById("stats-grid");
    if (!statsGrid) return;

    setInterval(async () => {
      try {
        const resp = await fetch("/api/dashboard");
        if (!resp.ok) return;
        const data = await resp.json();
        updateStats(data.stats);
        updateThroughput(data.throughput);
        updateSets(data.sets);
        updateRecentJobs(data.recent_jobs);
      } catch (_) {}
    }, POLL_INTERVAL);
  }

  function updateStats(stats) {
    for (const [key, val] of Object.entries(stats)) {
      const el = document.querySelector(`[data-stat="${key}"]`);
      if (el) el.textContent = val;
    }
    const tile = (key) => {
      const el = document.querySelector(`[data-stat="${key}"]`);
      return el && el.closest(".ui-stat");
    };
    // Red only while there are errors; the duplicates tile only when there are any
    const errors = tile("errors");
    if (errors) errors.classList.toggle("is-error", stats.errors > 0);
    const duplicates = tile("duplicates");
    if (duplicates) duplicates.hidden = !stats.duplicates;
    const spinner = document.querySelector('[data-stat-icon="processing"]');
    if (spinner) spinner.classList.toggle("ui-spin", stats.processing > 0);
  }

  function updateThroughput(throughput) {
    if (!throughput) return;
    for (const [key, val] of Object.entries(throughput)) {
      const el = document.querySelector(`[data-throughput="${key}"]`);
      if (el) el.textContent = val;
    }
    // ETA below the "queued" tile, hidden while there is none
    const eta = document.querySelector('[data-throughput="eta_label"]');
    if (eta) eta.hidden = !throughput.eta_label;
  }

  function updateSets(sets) {
    const tbody = document.getElementById("sets-grid");
    if (!tbody || !sets) return;
    const labels = JSON.parse(tbody.dataset.labels || "{}");
    dashReplaceRows(tbody, sets.map((s) => dashSetRow(s, labels)).join(""));
  }

  // Replaces the rows only when they changed, so focus and hover survive the poll.
  function dashReplaceRows(tbody, html) {
    if (tbody._dashHtml === html) return;
    tbody._dashHtml = html;
    tbody.innerHTML = html;
  }

  function dashSetRow(s, L) {
    const state = dashEscAttr(s.active ? L.active : L.inactive);
    const inputs = s.directories.map(
      (p) => `<span class="ui-cell-meta">${dashIcon("folder-input", 15)}<code class="ui-code">${esc(p)}</code></span>`
    );
    if (s.immich_input || s.immich_poll) {
      inputs.push(`<span class="ui-cell-meta${s.immich_poll ? " is-on" : ""}">${dashIcon("cloud-down", 15)}${esc(s.immich_poll ? L.poll_on : L.poll_off)}</span>`);
    }
    if (!inputs.length) inputs.push(`<span>${esc(L.no_inputs)}</span>`);
    const waiting = esc(L.queue.replace("{n}", s.queue));
    return `<tr${s.active ? "" : ' class="is-inactive"'}>`
      + `<th scope="row"><div class="ui-cell-name"><span class="ui-dot ${s.active ? "is-success" : "is-hollow"}" role="img" title="${state}" aria-label="${state}"></span>`
      + `<span class="dash-set-name"><strong>${esc(s.name)}</strong>${s.dry_run ? `<span class="ui-badge is-warning">${esc(L.dry_run)}</span>` : ""}</span></div></th>`
      + `<td class="dash-col-inputs"><div class="dash-set-inputs">${inputs.join("")}</div></td>`
      + `<td class="dash-set-queue">${s.queue ? `<span class="ui-badge is-info">${waiting}</span>` : waiting}</td>`
      + `<td class="ui-col-actions"><div class="ui-actions"><a class="ui-icon-btn" href="/settings?edit=${encodeURIComponent(s.id)}#section-sets"`
      + ` title="${dashEscAttr(L.edit)}" aria-label="${dashEscAttr(L.edit)}: ${dashEscAttr(s.name)}">${dashIcon("pencil", 18)}</a></div></td>`
      + `</tr>`;
  }

  function updateRecentJobs(jobs) {
    const tbody = document.getElementById("recent-jobs");
    if (!tbody || !jobs) return;
    const card = document.getElementById("recent-jobs-card");
    const empty = document.getElementById("recent-jobs-empty");
    if (card) card.hidden = !jobs.length;
    if (empty) empty.hidden = !!jobs.length;

    dashReplaceRows(tbody, jobs.map((j) => {
      const msg = j.error_message || "";
      return `<tr>`
        + `<td class="ui-col-icon">${statusBadge(j.status, true)}</td>`
        + `<th scope="row"><div class="dash-job"><a class="dash-job-name" href="/logs/job/${dashEscAttr(j.debug_key)}" title="${dashEscAttr(j.filename)}">${esc(j.filename || "—")}</a>`
        + `<span class="dash-job-key">${esc(j.debug_key || "—")}</span></div></th>`
        + `<td class="dash-col-step">${esc(j.current_step || "—")}</td>`
        + `<td class="cell-msg${j.status === "error" ? " is-error" : ""}" title="${dashEscAttr(msg)}">${esc(msg)}</td>`
        + `<td class="ui-num dash-col-time">${esc(j.updated_at)}</td>`
        + `</tr>`;
    }).join(""));
  }

  // Sprite icon, same markup as the icon() macro in dashboard.html
  function dashIcon(name, size) {
    return `<svg class="ui-i" width="${size}" height="${size}" aria-hidden="true" focusable="false"><use href="#ui-i-${name}"/></svg>`;
  }

  // esc() for attribute values: also the double quote
  function dashEscAttr(str) {
    return esc(str).replace(/"/g, "&quot;");
  }

  // ── Job Detail auto-refresh ──
  function initJobDetail() {
    const header = document.querySelector("[data-page='job-detail']");
    if (!header) return;
    const debugKey = header.dataset.debugKey;

    // "Expand all" / "Collapse all" for the step results
    const toggle = document.getElementById("steps-toggle");
    const steps = document.getElementById("step-results");
    if (toggle && steps) {
      const sync = () => {
        const all = Array.from(steps.querySelectorAll("details.ui-step"));
        const allOpen = all.length > 0 && all.every((d) => d.open);
        toggle.textContent = allOpen ? toggle.dataset.collapse : toggle.dataset.expand;
        toggle.setAttribute("aria-expanded", allOpen ? "true" : "false");
        toggle.hidden = all.length === 0;
      };
      toggle.addEventListener("click", () => {
        const all = Array.from(steps.querySelectorAll("details.ui-step"));
        const open = !all.every((d) => d.open);
        all.forEach((d) => { d.open = open; });
        sync();
      });
      // "toggle" does not bubble: listen in the capture phase
      steps.addEventListener("toggle", sync, true);
      steps.addEventListener("ma:steps-rendered", sync);
      sync();
    }

    setInterval(async () => {
      try {
        const resp = await fetch(`/logs/job/${debugKey}/json`);
        if (!resp.ok) return;
        const job = await resp.json();
        if (job.error === "not_found") return;
        updateJobDetail(job);
      } catch (_) {}
    }, POLL_INTERVAL);
  }

  function updateJobDetail(job) {
    // Status badge — only the main one tagged data-field="status".
    // Other .status-badge elements (e.g. the dry_run "Preview" badge)
    // must NOT be overwritten or the user sees them flipping to the
    // job status text on each poll cycle.
    const current = document.querySelector("[data-field='status']");
    if (current && current.dataset.status !== job.status) {
      const tpl = document.createElement("template");
      tpl.innerHTML = statusBadge(job.status);
      const fresh = tpl.content.firstElementChild;
      fresh.dataset.field = "status";
      current.replaceWith(fresh);
    }

    // Retry / delete: not while the job is waiting or running
    const actions = document.querySelector("[data-section='actions']");
    if (actions) actions.hidden = job.status === "queued" || job.status === "processing";

    // Current step (use data-field attribute)
    const stepEl = document.querySelector("[data-field='current_step']");
    if (stepEl) {
      const stepLabel = job.current_step_label ? ` — ${job.current_step_label}` : "";
      stepEl.textContent = (job.current_step || "—") + stepLabel;
    }

    // Target path (use data-field attribute)
    const targetEl = document.querySelector("[data-field='target_path']");
    if (targetEl) {
      targetEl.textContent = job.target_path || "—";
      targetEl.classList.toggle("is-empty", !job.target_path);
    }

    // Timestamps (use data-field attributes)
    const updatedEl = document.querySelector("[data-field='updated_at']");
    if (updatedEl) updatedEl.textContent = job.updated_at || "—";
    const completedEl = document.querySelector("[data-field='completed_at']");
    if (completedEl) completedEl.textContent = job.completed_at || "—";

    // Message: red "Fehler" only when the job failed, else a blue "Hinweis".
    // The alert is always in the page (hidden when empty).
    const msgEl = document.querySelector("[data-field='error_message']");
    if (msgEl) {
      const failed = job.status === "error";
      const box = msgEl.closest("[data-section='message']");
      const title = box.querySelector("[data-field='message_title']");
      const iconUse = box.querySelector("[data-field='message_icon']");
      msgEl.textContent = job.error_message || "";
      box.classList.toggle("is-error", failed);
      box.classList.toggle("is-info", !failed);
      if (title) title.textContent = failed ? title.dataset.labelError : title.dataset.labelNote;
      if (iconUse) iconUse.setAttribute("href", "#ui-i-" + (failed ? "alert-circle" : "info"));
      box.hidden = !job.error_message;
    }

    // Step results: rebuilt only when they changed; open steps stay open.
    // Mirrors the markup and the tone table of log_detail.html.
    const steps = document.getElementById("step-results");
    if (!steps) return;
    const sig = JSON.stringify(job.step_result || {});
    if (steps.dataset.sig === sig) return;
    steps.dataset.sig = sig;

    const L = JSON.parse(steps.dataset.labels || "{}");
    const labels = job.step_labels || {};
    const TONES = {
      error: ["is-error", "alert-circle"],
      warning: ["is-warning", "alert-triangle"],
      skipped: ["is-neutral", "pause-circle"],
      duplicate: ["is-warning", "copy"],
      dry_run: ["is-info", "eye"],
    };
    const icon = (name) =>
      `<svg class="ui-i" width="16" height="16" aria-hidden="true" focusable="false"><use href="#ui-i-${name}"/></svg>`;
    const el = (tag, cls, text) => {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    };
    const pre = (text, extra) => el("pre", "ui-pre ui-pre-wrap" + (extra ? " " + extra : ""), text);

    const wasOpen = {};
    steps.querySelectorAll("details.ui-step").forEach((d) => { wasOpen[d.dataset.step] = d.open; });
    const entries = Object.entries(job.step_result || {});
    steps.replaceChildren();
    if (!entries.length) {
      const empty = el("div", "ui-empty-inline job-steps-empty");
      empty.innerHTML = icon("clock");
      empty.appendChild(el("span", "", L.none || ""));
      steps.appendChild(empty);
    }
    for (const [step, result] of entries) {
      const isMap = result !== null && typeof result === "object" && !Array.isArray(result);
      const st = isMap ? result.status : undefined;
      const tone = !isMap ? ["is-neutral", "info"] : TONES[st] || ["is-success", "check-circle"];
      const toneLabel = !isMap ? "" : TONES[st] ? L[st] : L.ok;

      const d = el("details", "ui-step");
      d.dataset.step = step;
      d.open = step in wasOpen ? wasOpen[step] : st === "error";

      const sum = el("summary");
      const status = el("span", "ui-status " + tone[0]);
      status.innerHTML = icon(tone[1]);
      if (toneLabel) {
        status.title = toneLabel;
        status.appendChild(el("span", "ui-sr-only", toneLabel));
      }
      sum.append(status, el("span", "ui-step-code", step), el("span", "ui-step-label", labels[step] || step));
      const reason = isMap && typeof result.reason === "string" ? result.reason : "";
      if (reason) {
        const r = el("span", "step-reason", reason);
        r.title = reason;
        sum.appendChild(r);
      }
      sum.insertAdjacentHTML("beforeend",
        '<svg class="ui-i ui-step-chev" width="16" height="16" aria-hidden="true" focusable="false"><use href="#ui-i-chevron-down"/></svg>');

      const body = el("div", "ui-step-body");
      if (step === "IA-05" && isMap && result._context) {
        const images = result._images || 0;
        const meta = el("p", "step-ai-meta", (L.model || "") + ": ");
        meta.appendChild(el("strong", "", result._model));
        meta.appendChild(document.createTextNode(
          " · " + String(images !== 1 ? L.image_many : L.image_one).replace("{n}", images)));
        const shown = {};
        for (const [k, v] of Object.entries(result)) if (!k.startsWith("_")) shown[k] = v;
        body.append(meta,
          el("p", "step-ai-label", L.context), pre(result._context, "step-ai-context"),
          el("p", "step-ai-label", L.answer), pre(JSON.stringify(shown, null, 2)));
      } else {
        body.appendChild(pre(JSON.stringify(result, null, 2)));
      }
      d.append(sum, body);
      steps.appendChild(d);
    }
    steps.dispatchEvent(new Event("ma:steps-rendered"));
  }

  // ── Helpers ──
  // Status badge (icon + label) cloned from the copies base.html renders,
  // so the icons and translations live in one place (_status.html).
  function statusBadge(status, iconOnly) {
    const tpl = document.getElementById("status-badges");
    const variant = iconOnly ? ".is-icon" : ":not(.is-icon)";
    const badge = tpl && tpl.content.querySelector(`[data-status="${CSS.escape(status || "")}"]${variant}`);
    if (badge) return badge.outerHTML;
    return `<span class="status-badge status-${esc(status)}" data-status="${esc(status)}">${esc(status)}</span>`;
  }

  function esc(str) {
    if (str == null) return "";
    const div = document.createElement("div");
    div.textContent = String(str);
    return div.innerHTML;
  }

  // ── Lightbox ──
  function initLightbox() {
    const lightbox = document.getElementById("lightbox");
    const lightboxImg = document.getElementById("lightbox-img");
    if (!lightbox || !lightboxImg) return;

    document.addEventListener("click", function (e) {
      const trigger = e.target.closest(".lightbox-trigger");
      if (!trigger) return;
      e.preventDefault();
      e.stopPropagation();
      lightboxImg.src = trigger.dataset.fullsize || trigger.src;
      lightbox.classList.add("active");
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") lightbox.classList.remove("active");
    });
  }

  // Texts the shared helpers need, rendered by base.html (i18n).
  let uiStrings = null;
  function uiText(key) {
    if (!uiStrings) {
      const blob = document.getElementById("ui-strings");
      uiStrings = blob ? JSON.parse(blob.textContent) : {};
    }
    return uiStrings[key] || "";
  }

  // ── Menus: .ui-menu-wrap > button[aria-haspopup="menu"] + .ui-menu ──
  // Every .ui-menu-wrap on the page is wired up once. Click or Enter/Space
  // toggles, ArrowDown/ArrowUp on the button opens on the first/last item;
  // in the menu arrows, Home and End move between items, Tab closes it,
  // Escape closes it and returns focus to the button. A click elsewhere
  // closes it, and so does choosing an item (before the item's own handler
  // runs, so a confirmation dialog opens on a closed menu). The panel is
  // shifted or flipped so it stays inside the viewport.
  const openMenus = new Set();
  const VIEWPORT_MARGIN = 8;

  function initMenu(wrap) {
    const btn = wrap.querySelector(':scope > [aria-haspopup="menu"]');
    const menu = wrap.querySelector(":scope > .ui-menu");
    if (!btn || !menu) return;
    const items = () => Array.from(menu.querySelectorAll(".ui-menu-item:not(:disabled):not([hidden])"));
    const focusItem = (which) => {
      const list = items();
      if (list.length) list[which === "last" ? list.length - 1 : 0].focus();
    };
    const api = {
      wrap,
      close(returnFocus) {
        if (menu.hidden) return;
        menu.hidden = true;
        btn.setAttribute("aria-expanded", "false");
        menu.style.transform = menu.style.top = menu.style.bottom = "";
        openMenus.delete(api);
        if (returnFocus) btn.focus();
      },
      place() {
        menu.style.transform = menu.style.top = menu.style.bottom = "";
        const r = menu.getBoundingClientRect();
        const maxRight = document.documentElement.clientWidth - VIEWPORT_MARGIN;
        let dx = 0;
        if (r.right > maxRight) dx = maxRight - r.right;
        if (r.left + dx < VIEWPORT_MARGIN) dx = VIEWPORT_MARGIN - r.left;
        if (dx) menu.style.transform = `translateX(${Math.round(dx)}px)`;
        // Not enough room below but more above: open upwards
        const b = btn.getBoundingClientRect();
        if (r.bottom > innerHeight - VIEWPORT_MARGIN && b.top - VIEWPORT_MARGIN > innerHeight - b.bottom) {
          menu.style.top = "auto";
          menu.style.bottom = "calc(100% + 0.35rem)";
        }
      },
    };

    function open(focus) {
      closeMenus(api);
      if (menu.hidden) {
        menu.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        openMenus.add(api);
        api.place();
      }
      if (focus) focusItem(focus);
    }

    btn.addEventListener("click", (e) => {
      if (!menu.hidden) { api.close(false); return; }
      // e.detail is 0 for a click from the keyboard: then start on the first item
      open(e.detail === 0 ? "first" : null);
    });
    btn.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        open(e.key === "ArrowDown" ? "first" : "last");
      }
    });
    // Capture phase: the menu is closed before the item's own handler runs
    menu.addEventListener("click", (e) => {
      if (e.target.closest(".ui-menu-item")) api.close(true);
    }, true);
    menu.addEventListener("keydown", (e) => {
      const list = items();
      const i = list.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const n = list.length;
        if (!n) return;
        const step = e.key === "ArrowDown" ? 1 : -1;
        list[i < 0 ? (step > 0 ? 0 : n - 1) : (i + step + n) % n].focus();
      } else if (e.key === "Home" || e.key === "End") {
        e.preventDefault();
        focusItem(e.key === "Home" ? "first" : "last");
      } else if (e.key === "Tab") {
        // Back on the button first, so Tab / Shift+Tab continue from there
        api.close(true);
      }
    });
  }

  // Closes every open menu, except `keep`
  function closeMenus(keep) {
    openMenus.forEach((m) => { if (m !== keep) m.close(false); });
  }

  function initMenus() {
    document.querySelectorAll(".ui-menu-wrap").forEach(initMenu);
    document.addEventListener("click", (e) => {
      openMenus.forEach((m) => { if (!m.wrap.contains(e.target)) m.close(false); });
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || !openMenus.size) return;
      // Only one menu is open at a time
      openMenus.forEach((m) => m.close(true));
    });
    window.addEventListener("resize", () => openMenus.forEach((m) => m.place()));
  }

  // ── Confirmation dialog: MA.confirm({title, text, ok, danger}) → Promise<boolean> ──
  // One dialog.ui-dialog for every page, built on first use. Destructive
  // actions (danger) start on "Cancel" and get a red button; harmless ones
  // start on the action itself. Escape, the close button and "Cancel"
  // resolve false.
  let confirmDialog = null;

  function buildConfirmDialog() {
    const dlg = document.createElement("dialog");
    dlg.className = "ui-dialog";
    dlg.id = "ui-confirm";
    dlg.setAttribute("aria-labelledby", "ui-confirm-title");
    dlg.setAttribute("aria-describedby", "ui-confirm-text");
    // Fixed markup only; the texts are set with textContent
    dlg.innerHTML = '<form method="dialog" class="ui-dialog-shell">'
      + '<div class="ui-dialog-head"><div class="ui-dialog-head-text"><h2 class="ui-dialog-title" id="ui-confirm-title"></h2></div>'
      + '<button type="submit" value="cancel" class="ui-icon-btn" data-role="close">'
      + '<svg class="ui-i" width="18" height="18" aria-hidden="true" focusable="false"><use href="#ui-i-x"/></svg></button></div>'
      + '<div class="ui-dialog-body"><p class="ui-dialog-text" id="ui-confirm-text"></p></div>'
      + '<div class="ui-dialog-foot">'
      + '<button type="submit" value="cancel" class="ui-btn ui-btn-secondary" data-role="cancel"></button>'
      + '<button type="submit" value="ok" class="ui-btn ui-btn-primary" data-role="ok"></button>'
      + "</div></form>";
    const close = dlg.querySelector('[data-role="close"]');
    close.setAttribute("aria-label", uiText("close"));
    close.title = uiText("close");
    dlg.querySelector('[data-role="cancel"]').textContent = uiText("cancel");
    document.body.appendChild(dlg);
    return dlg;
  }

  function confirmAsk(opts) {
    const dlg = confirmDialog || (confirmDialog = buildConfirmDialog());
    dlg.querySelector("#ui-confirm-title").textContent = opts.title || "";
    dlg.querySelector("#ui-confirm-text").textContent = opts.text || "";
    const ok = dlg.querySelector('[data-role="ok"]');
    ok.textContent = opts.ok || uiText("ok");
    ok.className = "ui-btn " + (opts.danger ? "ui-btn-danger-solid" : "ui-btn-primary");
    dlg.returnValue = "";
    return new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      dlg.showModal();
      (opts.danger ? dlg.querySelector('[data-role="cancel"]') : ok).focus();
    });
  }

  // The options for MA.confirm from data-confirm-title/-text/-ok/-danger
  function confirmOptions(el) {
    return {
      title: el.dataset.confirmTitle, text: el.dataset.confirmText,
      ok: el.dataset.confirmOk, danger: !!el.dataset.confirmDanger,
    };
  }

  // ── Navbar dropdowns: one open at a time, closed by a click elsewhere or Escape ──
  function initDropdown(root, onOpen) {
    const toggle = root.querySelector(".quick-menu-toggle");
    const panel = root.querySelector(".quick-menu-panel");

    function setOpen(open) {
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open && onOpen) onOpen();
    }
    toggle.addEventListener("click", function () {
      setOpen(panel.hidden);
    });
    // No stopPropagation on the toggle: the click must reach the other
    // dropdowns' handlers so they close.
    document.addEventListener("click", function (e) {
      if (!panel.hidden && !root.contains(e.target)) setOpen(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !panel.hidden) { setOpen(false); toggle.focus(); }
    });
    return panel;
  }

  // ── Quick menu: language and theme, applied at once ──
  function initQuickMenu() {
    const root = document.getElementById("appearance-menu");
    if (!root) return;
    initDropdown(root).addEventListener("click", function (e) {
      const btn = e.target.closest("[data-appearance]");
      if (!btn || btn.classList.contains("active")) return;
      const body = {};
      body[btn.dataset.appearance] = btn.dataset.value;
      fetch("/settings/appearance", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      }).then(function () { location.reload(); });
    });
  }

  // ── Status menu: module health, marker green / amber / red ──
  function initStatusMenu() {
    const root = document.getElementById("status-menu");
    if (!root) return;
    const labels = JSON.parse(root.dataset.labels);
    const toggle = root.querySelector(".quick-menu-toggle");
    const srText = root.querySelector(".status-menu-sr");
    const summary = document.getElementById("status-menu-summary");
    const list = document.getElementById("status-menu-list");

    function render(data) {
      const counted = data.overall === "error" ? "error" : "misconfigured";
      const n = data.modules.filter(function (m) { return m.status === counted; }).length;
      const text = labels[data.overall].replace("{n}", n);
      root.dataset.state = data.overall;
      summary.textContent = text;
      toggle.title = labels.title + ": " + text;
      srText.textContent = toggle.title;
      list.innerHTML = data.modules
        .map(function (m) {
          const tag = m.href ? "a" : "div";
          const href = m.href ? ` href="${esc(m.href)}"` : "";
          return `<${tag} class="sm-row sm-${esc(m.status)}"${href}>
            <span class="sm-dot" aria-hidden="true"></span>
            <span class="sm-text">
              <span class="sm-name">${esc(m.label)}</span>
              <span class="sm-detail">${esc(m.detail)}</span>
            </span>
          </${tag}>`;
        })
        .join("");
    }

    async function refresh() {
      try {
        const resp = await fetch("/api/modules");
        if (resp.ok) render(await resp.json());
      } catch (_) {}
    }

    initDropdown(root, refresh);
    refresh();
    setInterval(refresh, 30000);
  }

  // ── Init ──
  initDashboard();
  initJobDetail();
  initLightbox();
  initMenus();
  initQuickMenu();
  initStatusMenu();

  // For the pages' own scripts
  window.MA = { confirm: confirmAsk, confirmOptions };
})();
