// MediaAssistant Frontend JS — Auto-Refresh

(function () {
  const POLL_INTERVAL = 5000;

  // ── Dashboard auto-refresh ──
  function initDashboard() {
    const statsGrid = document.getElementById("stats-grid");
    if (!statsGrid) return;

    setInterval(async () => {
      try {
        const resp = await fetch("/api/dashboard");
        if (!resp.ok) return;
        const data = await resp.json();
        updateStats(data.stats);
        updateModules(data.modules);
        updateRecentJobs(data.recent_jobs);
      } catch (_) {}
    }, POLL_INTERVAL);
  }

  function updateStats(stats) {
    for (const [key, val] of Object.entries(stats)) {
      const el = document.querySelector(`[data-stat="${key}"]`);
      if (el) el.textContent = val;
    }
  }

  function updateModules(modules) {
    const grid = document.getElementById("modules-grid");
    if (!grid) return;

    grid.innerHTML = modules
      .map((m) => {
        const dot =
          m.status === "ready"
            ? "●"
            : m.status === "error"
              ? "●"
              : m.status === "misconfigured"
                ? "●"
                : "○";
        return `<div class="module-card module-${m.status}">
          <div class="module-top">
            <span class="module-status">${dot}</span>
            <span class="module-name">${esc(m.label)}</span>
          </div>
          <div class="module-detail">${esc(m.detail)}</div>
        </div>`;
      })
      .join("");
  }

  function updateRecentJobs(jobs) {
    const tbody = document.getElementById("recent-jobs");
    if (!tbody) return;

    tbody.innerHTML = jobs
      .map(
        (j) => `<tr class="log-row log-${j.status}">
        <td><a href="/logs/job/${esc(j.debug_key)}" class="log-link-key"><code>${esc(j.debug_key || "—")}</code></a></td>
        <td>${esc(j.filename)}</td>
        <td><span class="status-badge status-${j.status}">${esc(j.status)}</span></td>
        <td>${esc(j.current_step || "—")}</td>
        <td class="cell-error">${esc(j.error_message || "")}</td>
        <td>${esc(j.updated_at)}</td>
      </tr>`
      )
      .join("");
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
    // Status badge
    const badges = document.querySelectorAll(".status-badge");
    badges.forEach((b) => {
      b.className = `status-badge status-${job.status}`;
      b.textContent = job.status;
    });

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

    // Error section — show/hide
    const errorSection = document.querySelector(".alert-error");
    if (errorSection) {
      errorSection.textContent = job.error_message || "";
      errorSection.closest(".detail-section").style.display = job.error_message
        ? ""
        : "none";
    }

    // Step results
    if (job.step_result) {
      let section = document.querySelector(".step-results");
      if (section) {
        section.innerHTML = Object.entries(job.step_result)
          .map(
            ([step, result]) => `<div class="step-item">
            <span class="step-code">${esc(step)}</span>
            <span class="step-label">${esc(job.step_labels && job.step_labels[step] || step)}</span>
            <pre class="step-data">${esc(JSON.stringify(result, null, 2))}</pre>
          </div>`
          )
          .join("");
      }
    }
  }

  // ── Helpers ──
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

  // ── Init ──
  initDashboard();
  initJobDetail();
  initLightbox();
})();
