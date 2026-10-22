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

    // Current step (use data-field attribute)
    const stepEl = document.querySelector("[data-field='current_step']");
    if (stepEl) {
      const stepLabel = job.current_step_label ? ` — ${job.current_step_label}` : "";
      stepEl.textContent = (job.current_step || "—") + stepLabel;
    }

    // Target path (use data-field attribute)
    const targetEl = document.querySelector("[data-field='target_path']");
    if (targetEl) targetEl.textContent = job.target_path || "—";

    // Timestamps (use data-field attributes)
    const updatedEl = document.querySelector("[data-field='updated_at']");
    if (updatedEl) updatedEl.textContent = job.updated_at || "—";
    const completedEl = document.querySelector("[data-field='completed_at']");
    if (completedEl) completedEl.textContent = job.completed_at || "—";

    // Message section: red "error" only when the job failed, else a note
    const msgEl = document.querySelector("[data-field='error_message']");
    if (msgEl) {
      const failed = job.status === "error";
      const section = msgEl.closest(".detail-section");
      const heading = section.querySelector("h2");
      msgEl.textContent = job.error_message || "";
      msgEl.classList.toggle("alert-error", failed);
      msgEl.classList.toggle("alert-info", !failed);
      heading.textContent = failed ? heading.dataset.labelError : heading.dataset.labelNote;
      section.style.display = job.error_message ? "" : "none";
    }

    // Step results
    if (job.step_result) {
      let section = document.querySelector(".step-results");
      if (section) {
        section.innerHTML = Object.entries(job.step_result)
          .map(
            ([step, result]) => {
              const label = esc(job.step_labels && job.step_labels[step] || step);
              if (step === "IA-05" && result && result._context) {
                const displayResult = {};
                for (const [k, v] of Object.entries(result)) {
                  if (!k.startsWith("_")) displayResult[k] = v;
                }
                return `<div class="step-item">
                  <span class="step-code">${esc(step)}</span>
                  <span class="step-label">${label}</span>
                  <div class="step-ai-detail">
                    <div style="margin-bottom:0.5rem;color:var(--text-muted);font-size:0.85rem;">
                      <strong>Modell:</strong> ${esc(result._model)} — <strong>${result._images}</strong> Bild${result._images > 1 ? "er" : ""}
                    </div>
                    <div style="margin-bottom:0.5rem;">
                      <strong>Kontext an KI:</strong>
                      <pre class="step-data" style="margin-top:0.25rem;color:var(--text-muted);">${esc(result._context)}</pre>
                    </div>
                    <strong>KI-Antwort:</strong>
                    <pre class="step-data" style="margin-top:0.25rem;">${esc(JSON.stringify(displayResult, null, 2))}</pre>
                  </div>
                </div>`;
              }
              return `<div class="step-item">
                <span class="step-code">${esc(step)}</span>
                <span class="step-label">${label}</span>
                <pre class="step-data">${esc(JSON.stringify(result, null, 2))}</pre>
              </div>`;
            }
          )
          .join("");
      }
    }
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
  initQuickMenu();
  initStatusMenu();
})();
