// MediaAssistant Frontend JS — Auto-Refresh

(function () {
  const POLL_INTERVAL = 5000;

  // ── Dashboard auto-refresh ──
  // The rows rendered here mirror the macros set_row / job_row in
  // templates/dashboard.html: change both together.
  let dashHistory = null; // expandable rows of "Letzte Verarbeitungen"

  function initDashboard() {
    const statsGrid = document.getElementById("stats-grid");
    if (!statsGrid) return;
    const recent = document.getElementById("recent-jobs");
    if (recent) dashHistory = histRows(recent);

    setInterval(async () => {
      try {
        const resp = await fetch("/api/dashboard");
        if (!resp.ok) return;
        const data = await resp.json();
        updateStats(data.stats);
        updateThroughput(data.throughput);
        updateSets(data.sets);
        await updateRecentJobs(data.recent_jobs);
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

  // Module blocks per job, refetched only for jobs that changed since the last poll
  const dashBlocks = new Map(); // debug_key → {seen, signature, modules, summary}

  async function updateRecentJobs(jobs) {
    const tbody = document.getElementById("recent-jobs");
    if (!tbody || !jobs) return;
    const card = document.getElementById("recent-jobs-card");
    const empty = document.getElementById("recent-jobs-empty");
    if (card) card.hidden = !jobs.length;
    if (empty) empty.hidden = !!jobs.length;

    const seen = (j) => `${j.status}|${j.current_step}|${j.updated_at}`;
    const stale = jobs.filter((j) => j.debug_key && (dashBlocks.get(j.debug_key) || {}).seen !== seen(j));
    if (stale.length) {
      try {
        const keys = stale.map((j) => encodeURIComponent(j.debug_key)).join(",");
        const resp = await fetch(`/api/history/modules?keys=${keys}`, {headers: {"Accept": "application/json"}});
        if (resp.ok) {
          const found = (await resp.json()).jobs || {};
          for (const j of stale) {
            if (found[j.debug_key]) dashBlocks.set(j.debug_key, Object.assign({seen: seen(j)}, found[j.debug_key]));
          }
        }
      } catch (_) { /* rows keep their last blocks */ }
    }

    const html = jobs.map((j) => {
      const msg = j.error_message || "";
      const blocks = dashBlocks.get(j.debug_key);
      // Not fetched (yet): keep the strip the row already shows
      const old = blocks ? null : tbody.querySelector(`tr.hist-row[data-key="${CSS.escape(j.debug_key || "")}"] .mod-strip`);
      const strip = blocks ? histStripHTML(blocks.modules, blocks.summary) : (old ? old.outerHTML : "");
      const sig = blocks ? blocks.signature : (old ? old.closest("tr").dataset.sig : "");
      return `<tr class="hist-row" data-key="${dashEscAttr(j.debug_key)}" data-sig="${dashEscAttr(sig)}">`
        + `<td class="ui-col-icon">${statusBadge(j.status, true)}</td>`
        + `<th scope="row"><div class="dash-job"><a class="dash-job-name" href="/logs/job/${dashEscAttr(j.debug_key)}" title="${dashEscAttr(j.filename)}">${esc(j.filename || "—")}</a>`
        + `<span class="dash-job-key">${esc(j.debug_key || "—")}</span></div></th>`
        + `<td class="hist-col-mods dash-col-mods">${strip}</td>`
        + `<td class="cell-msg${j.status === "error" ? " is-error" : ""}" title="${dashEscAttr(msg)}">${esc(msg)}</td>`
        + `<td class="ui-num dash-col-time">${esc(j.updated_at)}</td>`
        + `<td class="ui-col-actions hist-col-toggle">${histToggleHTML(j.filename || j.debug_key)}</td>`
        + `</tr>`;
    }).join("");
    // Focus on a chevron survives the re-render
    const focused = tbody.contains(document.activeElement) && document.activeElement.closest(".hist-toggle")
      ? document.activeElement.closest("tr.hist-row").dataset.key : null;
    if (tbody._dashHtml !== html) {
      dashReplaceRows(tbody, html);
      if (dashHistory) dashHistory.reattach();
      if (focused) {
        const btn = tbody.querySelector(`tr.hist-row[data-key="${CSS.escape(focused)}"] .hist-toggle`);
        if (btn) btn.focus();
      }
    } else if (dashHistory) {
      dashHistory.refresh();
    }
  }

  // Sprite icon, same markup as the icon() macro in dashboard.html
  function dashIcon(name, size) {
    return `<svg class="ui-i" width="${size}" height="${size}" aria-hidden="true" focusable="false"><use href="#ui-i-${name}"/></svg>`;
  }

  // esc() for attribute values: also the double quote
  function dashEscAttr(str) {
    return esc(str).replace(/"/g, "&quot;");
  }

  // ── Data safety: module strip, expandable rows, ledger, copies ──
  // Data comes from job_view.py through /api/history/*. The strip and the
  // chevron mirror the macros in templates/_modules.html: change both
  // together. User data goes in with textContent only.
  let histStrings = null;
  function histText(key, vars) {
    if (!histStrings) {
      const blob = document.getElementById("history-strings");
      histStrings = blob ? JSON.parse(blob.textContent) : {};
    }
    let s = histStrings[key] || "";
    for (const [k, v] of Object.entries(vars || {})) s = s.split("{" + k + "}").join(String(v));
    return s;
  }

  // Same text as block_title() in _modules.html; an outdated block (b.stale:
  // a predecessor changed since) says so, and its tooltip explains it
  function histBlockTitle(b) {
    return `${b.module} ${b.label}: ${b.status_label}` + (b.stale ? ` · ${histText("stale")}` : "")
      + (b.detail ? ` — ${b.detail}` : "");
  }

  function histBlockTip(b) {
    return histBlockTitle(b) + (b.stale ? "\n" + histText("stale_hint") : "");
  }

  function histStripHTML(blocks, summary) {
    return `<span class="mod-strip" role="img" aria-label="${dashEscAttr(summary)}">`
      + (blocks || []).map((b) => `<span class="mod-b" data-s="${dashEscAttr(b.status)}"${b.stale ? " data-stale" : ""}`
        + ` title="${dashEscAttr(histBlockTip(b))}"></span>`).join("")
      + `</span>`;
  }

  function histToggleHTML(name) {
    const label = histText("expand");
    return `<button type="button" class="ui-icon-btn ui-icon-btn-sm hist-toggle" aria-expanded="false" data-name="${dashEscAttr(name)}"`
      + ` aria-label="${dashEscAttr(label + ": " + name)}" title="${dashEscAttr(label)}">${dashIcon("chevron-down", 16)}</button>`;
  }

  function histEl(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // Element with a sprite icon in front of an optional text
  function histIconEl(tag, cls, icon, text, size, spin) {
    const e = histEl(tag, cls);
    e.innerHTML = `<svg class="ui-i${spin ? " ui-spin" : ""}" width="${size || 14}" height="${size || 14}" aria-hidden="true" focusable="false"><use href="#ui-i-${icon}"/></svg>`;
    if (text) e.appendChild(document.createTextNode(text));
    return e;
  }

  function histBadge(text, tone) {
    return histEl("span", "ui-badge" + (tone ? " " + tone : ""), text);
  }

  function histState(text, isError) {
    const box = histIconEl("div", "hist-panel-state" + (isError ? " is-error" : ""),
      isError ? "alert-circle" : "loader", null, 16, !isError);
    box.appendChild(histEl("span", "", text));
    return box;
  }

  function histSection(title, body, count) {
    const sec = histEl("section", "hist-sec");
    const h = histEl("h3", "hist-sec-title", title);
    if (count) h.appendChild(histEl("span", "ui-count", String(count)));
    sec.append(h, body);
    return sec;
  }

  function histModules(blocks) {
    const ol = histEl("ol", "hist-mods");
    for (const b of blocks || []) {
      const li = histEl("li", "hist-mod");
      li.dataset.s = b.status;
      const st = histIconEl("span", "ui-status " + b.tone, b.icon, null, 14, b.status === "running");
      st.title = b.status_label;
      st.appendChild(histEl("span", "ui-sr-only", b.status_label));
      const detail = histEl("span", "hist-mod-detail", b.detail || "");
      if (b.detail) detail.title = b.detail;
      li.dataset.module = b.module;
      const meta = histEl("span", "hist-mod-meta", b.duration_s != null ? histSeconds(b.duration_s) : "");
      const label = histEl("span", "hist-mod-label", b.label);
      if (b.stale) {
        li.dataset.stale = "";
        const mark = histBadge(histText("stale"));
        mark.classList.add("hist-badge-dashed", "hist-badge-xs", "hist-mod-stale");
        mark.title = histText("stale_hint");
        label.appendChild(mark);
      }
      li.append(st, histEl("span", "hist-mod-code", b.module), label, detail, meta);
      // "2×" opens the earlier attempts below the row
      const n = (b.earlier || []).length;
      if (n) {
        const label = n === 1 ? histText("attempts_one") : histText("attempts_many", {n: n});
        const btn = histEl("button", "hist-mod-attempts", (b.attempts || n + 1) + "×");
        btn.insertAdjacentHTML("beforeend", dashIcon("chevron-down", 12));
        btn.type = "button";
        btn.title = label;
        btn.setAttribute("aria-label", label + ": " + b.module + " " + b.label);
        btn.setAttribute("aria-expanded", "false");
        const list = histAttemptList(b);
        list.classList.add("hist-runs");
        list.hidden = true;
        btn.addEventListener("click", () => {
          list.hidden = !list.hidden;
          btn.setAttribute("aria-expanded", String(!list.hidden));
        });
        if (meta.textContent) meta.appendChild(document.createTextNode(" · "));
        meta.appendChild(btn);
        li.appendChild(list);
      }
      ol.appendChild(li);
    }
    return ol;
  }

  function histSeconds(s) {
    return s < 10 ? s.toFixed(1) + " s" : Math.round(s) + " s";
  }

  // Earlier attempts of a module (module_runs): status, time, error
  function histAttemptList(b) {
    const ol = histEl("ol", "hist-runs-list");
    for (const a of b.earlier || []) {
      const li = histEl("li", "hist-run");
      li.dataset.s = a.status;
      const st = histIconEl("span", "ui-status " + a.tone, a.icon, null, 13);
      st.title = a.status_label;
      st.appendChild(histEl("span", "ui-sr-only", a.status_label));
      const head = histEl("div", "hist-run-head");
      head.append(histEl("strong", "hist-run-title", histText("attempt_label", {n: a.attempt})),
        histEl("span", "hist-run-status", a.status_label));
      if (a.trigger_label) head.appendChild(histBadge(a.trigger_label));
      if (a.superseded) head.appendChild(histEl("span", "hist-run-meta", histText("attempt_replaced")));
      const when = a.finished_at || a.started_at;
      if (when || a.duration_s != null) {
        head.appendChild(histEl("time", "hist-run-time",
          [when, a.duration_s != null ? histSeconds(a.duration_s) : null].filter(Boolean).join(" · ")));
      }
      const body = histEl("div", "hist-run-body");
      body.appendChild(head);
      if (a.error) {
        const err = histEl("p", "hist-run-error", a.error);
        err.title = a.error;
        body.appendChild(err);
      }
      li.append(st, body);
      ol.appendChild(li);
    }
    return ol;
  }


  // Timeline of a job: the intake ("Erfasst") first, then the ledger oldest
  // first. Consecutive housekeeping (op.housekeeping, see job_view) folds
  // into one grey line that opens on click.
  function histOps(ops, intake) {
    if ((!ops || !ops.length) && !intake) return histEl("p", "hist-sec-empty", histText("ops_empty"));
    const ol = histEl("ol", "hist-ops");
    if (intake) ol.appendChild(histIntakeItem(intake));
    // The API lists the newest operations first (and caps the count there);
    // shown oldest first, the timeline follows the order of the modules.
    let group = [];
    const flush = () => {
      if (group.length) ol.appendChild(histHousekeeping(group));
      group = [];
    };
    for (const op of (ops || []).slice().reverse()) {
      if (op.housekeeping) { group.push(op); continue; }
      flush();
      ol.appendChild(histOpItem(op));
    }
    flush();
    return ol;
  }

  function histIntakeItem(it) {
    const li = histEl("li", "hist-op hist-op-intake");
    li.dataset.state = it.reconstructed ? "reconstructed" : "intake";
    const tile = histIconEl("span", "ui-icon-tile ui-icon-tile-sm is-accent", it.icon, null, 14);
    const body = histEl("div", "hist-op-body");
    const head = histEl("div", "hist-op-head");
    head.appendChild(histEl("strong", "hist-op-title", it.title));
    if (it.source_label) head.appendChild(histBadge(it.source_label, "is-accent"));
    if (it.reconstructed) {
      const mark = histBadge(it.reconstructed_label);
      mark.classList.add("hist-badge-dashed");
      mark.title = it.reconstructed_note;
      head.appendChild(mark);
    }
    head.appendChild(histEl("time", "hist-op-time", it.created_at || ""));
    body.appendChild(head);
    if (it.location) {
      const path = histEl("div", "hist-op-path");
      if (it.location_kind_label) path.appendChild(histBadge(it.location_kind_label));
      path.appendChild(histEl("code", "ui-path", it.location));
      body.appendChild(path);
    }
    const meta = histEl("div", "hist-op-meta");
    if (it.set_name) meta.appendChild(histEl("span", "", histText("intake_set", {name: it.set_name})));
    if (it.input_name) meta.appendChild(histEl("span", "", histText("intake_input", {name: it.input_name})));
    if (it.hash) {
      const hash = histIconEl("span", "ui-cell-meta", "hash", it.hash_label + " ", 13);
      hash.appendChild(histEl("code", "hist-op-hash", it.hash + "…"));
      meta.appendChild(hash);
    }
    if (meta.childNodes.length) body.appendChild(meta);
    if (it.reconstructed && it.reconstructed_note) body.appendChild(histEl("p", "hist-op-note", it.reconstructed_note));
    li.append(tile, body);
    return li;
  }

  function histHousekeeping(group) {
    const li = histEl("li", "hist-op hist-op-group");
    li.dataset.group = String(group[0].id);
    const tile = histIconEl("span", "ui-icon-tile ui-icon-tile-sm is-neutral", "sparkles", null, 13);
    const body = histEl("div", "hist-op-body");
    const det = histEl("details", "hist-hk");
    const sum = histEl("summary", "hist-hk-sum");
    sum.title = histText("housekeeping_hint");
    const last = group[group.length - 1];
    sum.append(histEl("span", "hist-hk-label", group.length === 1 ? histText("housekeeping_one")
      : histText("housekeeping_many", {n: group.length})),
      histEl("span", "hist-hk-kinds", Array.from(new Set(group.map((o) => o.op_label))).join(", ")),
      histEl("time", "hist-op-time", last.finished_at || last.created_at || ""));
    const list = histEl("ol", "hist-ops hist-hk-list");
    for (const op of group) list.appendChild(histOpItem(op, true));
    det.append(sum, list);
    body.appendChild(det);
    li.append(tile, body);
    return li;
  }

  // Re-render `box` with `nodes`; housekeeping groups that were open stay open
  function histReplaceKeepOpen(box, ...nodes) {
    const open = new Set(Array.from(box.querySelectorAll(".hist-op-group[data-group] > .hist-op-body > details[open]"))
      .map((d) => d.closest(".hist-op-group").dataset.group));
    const runsOpen = new Set(Array.from(box.querySelectorAll(".hist-mod[data-module] > .hist-runs:not([hidden])"))
      .map((l) => l.closest(".hist-mod").dataset.module));
    box.replaceChildren(...nodes);
    for (const id of open) {
      const det = box.querySelector(`.hist-op-group[data-group="${CSS.escape(id)}"] > .hist-op-body > details`);
      if (det) det.open = true;
    }
    for (const mod of runsOpen) {
      const btn = box.querySelector(`.hist-mod[data-module="${CSS.escape(mod)}"] .hist-mod-attempts`);
      if (btn) btn.click();
    }
  }

  // One ledger row; `muted` inside a housekeeping group (grey, no state badge)
  function histOpItem(op, muted) {
    const li = histEl("li", "hist-op" + (muted ? " is-muted" : ""));
    li.dataset.state = op.state;
    const tile = histIconEl("span", "ui-icon-tile ui-icon-tile-sm " + (muted ? "is-neutral" : op.tone), op.icon, null,
      muted ? 12 : 14);
    tile.title = op.state_label;
    const body = histEl("div", "hist-op-body");
    const head = histEl("div", "hist-op-head");
    head.appendChild(histEl("strong", "hist-op-title", op.op_label));
    if (!muted) head.appendChild(histBadge(op.state_label, op.tone));
    head.appendChild(histEl("time", "hist-op-time", op.finished_at || op.created_at || ""));
    body.appendChild(head);
    if (op.src || op.dst) {
      const path = histEl("div", "hist-op-path");
      if (op.src) {
        if (op.src_kind_label) path.appendChild(histBadge(op.src_kind_label));
        path.appendChild(histEl("code", "ui-path", op.src));
      }
      const toOther = op.dst && op.dst !== op.src;
      if (toOther || (op.dst_kind && op.dst_kind !== op.src_kind)) {
        path.appendChild(histEl("span", "hist-op-arrow", "→"));
        if (op.dst_kind_label) path.appendChild(histBadge(op.dst_kind_label));
        if (toOther) path.appendChild(histEl("code", "ui-path", op.dst));
      }
      body.appendChild(path);
    }
    const meta = histEl("div", "hist-op-meta");
    if (op.hash) {
      meta.appendChild(histIconEl("span", "ui-cell-meta " + (op.hash === "verified" ? "is-ok" : "is-changed"),
        op.hash === "verified" ? "check" : "pencil", op.hash_label, 13));
    }
    for (const text of [op.actor_label, op.reason, op.note]) if (text) meta.appendChild(histEl("span", "", text));
    if (meta.childNodes.length) body.appendChild(meta);
    if (op.error) body.appendChild(histEl("p", "hist-op-error", op.error));
    li.append(tile, body);
    return li;
  }

  // Tags and albums MA wrote (job_view.job_labels), as sections
  function histLabels(labels) {
    if (!labels) return [];
    const tags = labels.tags || [];
    const list = tags.length ? histEl("div", "ui-tags hist-tags") : histEl("p", "hist-sec-empty", histText("labels_tags_empty"));
    for (const name of tags) list.appendChild(histEl("span", "ui-tag", name));
    const sec = histSection(labels.tags_title, list, tags.length);
    sec.classList.add("hist-sec-labels");
    if (labels.planned_label) sec.firstChild.appendChild(histBadge(labels.planned_label, "is-info"));
    const out = [sec];
    const albums = labels.albums || [];
    if (albums.length) {
      const box = histEl("div", "ui-tags hist-tags");
      for (const a of albums) {
        const chip = histIconEl("span", "ui-tag hist-album" + (a.created ? " is-success" : ""), "images", a.name, 13);
        if (a.created) {
          const mark = histBadge(histText("labels_album_new"), "is-success");
          mark.classList.add("hist-badge-xs");
          chip.title = histText("labels_album_new_title");
          chip.appendChild(mark);
        }
        box.appendChild(chip);
      }
      const asec = histSection(histText("labels_albums"), box, albums.length);
      asec.classList.add("hist-sec-labels");
      out.push(asec);
    }
    return out;
  }

  const COPY_ICONS = {present: "check-circle", trashed: "trash", missing: "alert-circle", deleted: "x-circle",
    moved: "arrow-right", locked: "lock"};

  // opts.restore(copy, button): adds a "Wiederherstellen" button to trashed copies.
  // Copies that moved on or are gone for good fold away below the current ones.
  function histCopies(copies, opts) {
    if (!copies || !copies.length) return histEl("p", "hist-sec-empty", histText("copies_empty"));
    const past = copies.filter((c) => c.state === "moved" || c.state === "deleted");
    const current = copies.filter((c) => !past.includes(c));
    const list = histCopyList(current, opts);
    if (!past.length) return list;
    const box = histEl("div", "hist-copies-wrap");
    const more = histEl("details", "hist-copies-past");
    more.appendChild(histEl("summary", "", past.length === 1 ? histText("copies_older_one")
      : histText("copies_older", {n: past.length})));
    more.appendChild(histCopyList(past, opts));
    if (current.length) box.appendChild(list);
    box.appendChild(more);
    return box;
  }

  function histCopyList(copies, opts) {
    const ul = histEl("ul", "hist-copies");
    for (const c of copies) {
      const li = histEl("li", "hist-copy");
      li.dataset.state = c.state;
      const head = histEl("div", "hist-copy-head");
      head.append(histBadge(c.kind_label), histIconEl("span", "ui-status " + c.tone, COPY_ICONS[c.state] || "circle", c.state_label, 14));
      if (c.shared && c.owner_key) head.appendChild(histEl("span", "hist-copy-meta", histText("shared_from", {key: c.owner_key})));
      head.appendChild(histEl("span", "hist-copy-meta",
        c.verified_at ? histText("verified_at", {when: c.verified_at}) : histText("never_verified")));
      li.append(head, histEl("code", "ui-path", c.location));
      if (c.state === "trashed") {
        const trash = histEl("div", "hist-copy-trash");
        if (c.trash_location) trash.append(histBadge(histText("kind_trash")), histEl("code", "ui-path", c.trash_location));
        if (c.remaining_label) trash.appendChild(histBadge(c.remaining_label, c.remaining_days != null && c.remaining_days <= 3 ? "is-warning" : ""));
        if (opts && opts.restore && c.can_restore) {
          const btn = histIconEl("button", "ui-btn ui-btn-secondary ui-btn-xs", "retry", histText("restore"), 14);
          btn.type = "button";
          btn.setAttribute("aria-label", histText("restore") + ": " + (c.name || c.location));
          btn.addEventListener("click", () => opts.restore(c, btn));
          trash.appendChild(btn);
        }
        li.appendChild(trash);
      }
      ul.appendChild(li);
    }
    return ul;
  }

  // Expanded row content: modules, tags and albums left; timeline and copies right
  function histPanel(data) {
    const panel = histEl("div", "hist-panel");
    const grid = histEl("div", "hist-panel-grid");
    const main = histEl("div", "hist-panel-side");
    main.append(histSection(histText("modules"), histModules(data.modules)), ...histLabels(data.labels));
    const side = histEl("div", "hist-panel-side");
    side.append(histSection(histText("ops_title"), histOps(data.ops, data.intake), (data.ops || []).length),
      histSection(histText("copies_title"), histCopies(data.copies), (data.copies || []).length));
    grid.append(main, side);
    const foot = histEl("div", "hist-panel-foot");
    const open = histEl("a", "ui-btn ui-btn-ghost ui-btn-sm", histText("open_job"));
    open.href = "/logs/job/" + encodeURIComponent(data.debug_key);
    open.insertAdjacentHTML("beforeend", dashIcon("chevron-right", 14));
    foot.appendChild(open);
    panel.append(grid, foot);
    return panel;
  }

  async function histFetchJob(key) {
    const resp = await fetch(`/api/history/job/${encodeURIComponent(key)}`, {headers: {"Accept": "application/json"}});
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return resp.json();
  }

  // Expandable rows: tbody rows `tr.hist-row[data-key][data-sig]` with a
  // `.hist-toggle` button. A click on the row or the chevron opens a detail
  // row below it, filled lazily from /api/history/job/<key>. After the rows
  // were re-rendered (live refresh), reattach() puts the open panels back and
  // reloads those whose job changed (data-sig).
  function histRows(tbody) {
    const open = new Map(); // key → {panel, sig, loading}
    const rowOf = (key) => tbody.querySelector(`tr.hist-row[data-key="${CSS.escape(key)}"]`);
    const columns = () => {
      const head = tbody.closest("table").querySelector("thead tr");
      return head ? head.children.length : 1;
    };

    function setToggle(row, isOpen) {
      row.classList.toggle("is-open", isOpen);
      const btn = row.querySelector(".hist-toggle");
      if (!btn) return;
      const label = histText(isOpen ? "collapse" : "expand");
      btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
      btn.setAttribute("aria-label", label + ": " + (btn.dataset.name || row.dataset.key));
      btn.title = label;
      if (isOpen) btn.setAttribute("aria-controls", "hist-d-" + row.dataset.key);
      else btn.removeAttribute("aria-controls");
    }

    function place(key, entry) {
      const row = rowOf(key);
      if (!row) return false;
      let detail = row.nextElementSibling;
      if (!detail || !detail.classList.contains("hist-detail") || detail.dataset.for !== key) {
        detail = histEl("tr", "hist-detail");
        detail.dataset.for = key;
        detail.id = "hist-d-" + key;
        const cell = histEl("td");
        cell.colSpan = columns();
        detail.appendChild(cell);
        row.after(detail);
      }
      if (entry.panel.parentNode !== detail.firstChild) detail.firstChild.appendChild(entry.panel);
      setToggle(row, true);
      return true;
    }

    async function load(key, entry) {
      entry.loading = true;
      try {
        const data = await histFetchJob(key);
        entry.sig = data.signature;
        histReplaceKeepOpen(entry.panel, ...histPanel(data).childNodes);
      } catch (e) {
        if (!entry.sig) entry.panel.replaceChildren(histState(histText("load_failed") + " (" + e.message + ")", true));
      } finally {
        entry.loading = false;
      }
    }

    function close(key) {
      open.delete(key);
      const detail = tbody.querySelector(`tr.hist-detail[data-for="${CSS.escape(key)}"]`);
      if (detail) detail.remove();
      const row = rowOf(key);
      if (row) setToggle(row, false);
    }

    function toggle(key) {
      if (open.has(key)) { close(key); return; }
      const entry = {panel: histEl("div", "hist-panel"), sig: null, loading: false};
      entry.panel.appendChild(histState(histText("loading")));
      open.set(key, entry);
      place(key, entry);
      load(key, entry);
    }

    tbody.addEventListener("click", (e) => {
      const row = e.target.closest("tr.hist-row");
      if (!row || !tbody.contains(row)) return;
      const btn = e.target.closest(".hist-toggle");
      // Links and other controls in the row keep their own meaning
      if (!btn && e.target.closest("a, button, input, select, textarea, label, summary")) return;
      // Selecting text is not a click on the row
      if (!btn && window.getSelection && String(window.getSelection()) !== "") return;
      toggle(row.dataset.key);
    });

    function refresh() {
      for (const [key, entry] of open) {
        const row = rowOf(key);
        if (row && !entry.loading && entry.sig && row.dataset.sig && row.dataset.sig !== entry.sig) load(key, entry);
      }
    }

    return {
      reattach() {
        for (const [key, entry] of Array.from(open)) {
          if (!place(key, entry)) open.delete(key); // the job left the list
        }
        refresh();
      },
      refresh,
    };
  }

  // Restore one trashed copy after a confirmation. Resolves to null when
  // cancelled, else {ok, title, text} for histAlert.
  async function histRestore(copy, btn) {
    const isImmich = copy.kind === "immich";
    const dir = (copy.location || "").replace(/\/[^/]*$/, "") || "/";
    const yes = await confirmAsk({
      title: histText("restore_title"),
      text: isImmich ? histText("restore_immich", {id: copy.location})
        : histText("restore_local", {file: copy.name || copy.location, dir: dir}),
      ok: histText("restore"),
    });
    if (!yes) return null;
    if (btn) { btn.disabled = true; btn.setAttribute("aria-busy", "true"); }
    try {
      const resp = await fetch(`/api/history/copies/${encodeURIComponent(copy.id)}/restore`, {
        method: "POST", credentials: "same-origin", headers: {"Accept": "application/json"},
      });
      let data = {};
      try { data = await resp.json(); } catch (_) {}
      if (resp.ok && data.ok) {
        return {ok: true, title: histText("restore_done", {location: data.location || copy.location}),
          text: (data.notes || []).join(" · ")};
      }
      return {ok: false, title: histText(data.refused ? "restore_refused" : "restore_failed"),
        text: data.error || "HTTP " + resp.status};
    } catch (e) {
      return {ok: false, title: histText("restore_failed"), text: e.message};
    } finally {
      if (btn) { btn.disabled = false; btn.removeAttribute("aria-busy"); }
    }
  }

  // Result of an action as a ui-alert in `box` (replaces the previous one)
  function histAlert(box, result) {
    if (!box) return;
    box.replaceChildren();
    if (!result) return;
    const alert = histIconEl("div", "ui-alert ui-alert-sm " + (result.ok ? "is-success" : "is-error"),
      result.ok ? "check-circle" : "alert-circle", null, 16);
    alert.setAttribute("role", result.ok ? "status" : "alert");
    const body = histEl("div", "ui-alert-body");
    body.appendChild(histEl("p", "ui-alert-title", result.title));
    if (result.text) body.appendChild(histEl("p", "ui-alert-text", result.text));
    const close = histIconEl("button", "ui-icon-btn ui-icon-btn-sm", "x", null, 16);
    close.type = "button";
    close.setAttribute("aria-label", uiText("close"));
    close.title = uiText("close");
    close.addEventListener("click", () => box.replaceChildren());
    alert.append(body, close);
    box.appendChild(alert);
  }

  // Expandable rows of the job log (server-rendered, no live refresh)
  function initHistoryTables() {
    document.querySelectorAll("tbody[data-history-rows]").forEach((tbody) => {
      if (tbody.id !== "recent-jobs") histRows(tbody);
    });
  }

  // ── Job Detail auto-refresh ──
  function initJobDetail() {
    const header = document.querySelector("[data-page='job-detail']");
    if (!header) return;
    const debugKey = header.dataset.debugKey;

    // The back target came from the Referer: put it into the address, so a
    // reload, and a job opened from here (whose Referer this address is),
    // still lead back to where the user came from (job_view.back_link).
    if (header.dataset.back) {
      try {
        const url = new URL(location.href);
        if (!url.searchParams.get("back")) {
          url.searchParams.set("back", header.dataset.back);
          history.replaceState(history.state, "", url.pathname + url.search + url.hash);
        }
      } catch (_) {}
    }

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

    initJobHistory(debugKey);
  }

  // Module bar, ledger ("Verlauf") and copies ("Exemplare") of the job detail
  // page, all from /api/history/job/<key>. The bar mirrors module_bar() in
  // templates/_modules.html.
  function initJobHistory(debugKey) {
    const bar = document.getElementById("job-module-bar");
    const opsBox = document.getElementById("job-ops");
    const copiesBox = document.getElementById("job-copies");
    const alertBox = document.getElementById("job-copies-alert");
    const opsCount = document.getElementById("job-ops-count");
    const copiesCount = document.getElementById("job-copies-count");
    const labelsBox = document.getElementById("job-labels");
    if (!bar && !opsBox && !copiesBox) return;

    // A module block opens its step result below
    if (bar) {
      bar.addEventListener("click", (e) => {
        const cell = e.target.closest(".mod-cell");
        if (!cell) return;
        const step = document.querySelector(`#step-results details.ui-step[data-step="${CSS.escape(cell.dataset.step)}"]`);
        const target = step || document.getElementById("step-results");
        if (!target) return;
        if (step) {
          step.open = true;
          step.classList.add("is-flash");
          setTimeout(() => step.classList.remove("is-flash"), 1200);
        }
        target.scrollIntoView({block: "center", behavior: "smooth"});
        const summary = step && step.querySelector("summary");
        if (summary) summary.focus({preventScroll: true});
      });
    }

    let last = null;
    async function restore(copy, btn) {
      const result = await histRestore(copy, btn);
      if (!result) return;
      histAlert(alertBox, result);
      last = null;
      await refresh();
    }

    function renderBar(blocks, summary) {
      if (!bar) return;
      bar.setAttribute("aria-label", summary);
      for (const b of blocks) {
        const cell = bar.querySelector(`.mod-cell[data-step="${CSS.escape(b.module)}"]`);
        if (!cell) continue;
        const block = cell.querySelector(".mod-b");
        cell.dataset.s = b.status;
        cell.title = histBlockTip(b);
        block.dataset.s = b.status;
        block.toggleAttribute("data-stale", !!b.stale);
        cell.querySelector(".ui-sr-only").textContent = histBlockTitle(b);
      }
    }

    // Earlier attempts of a module at the top of its step result; again
    // after the step results were rebuilt (updateJobDetail)
    let attemptBlocks = null;
    const stepsBox = document.getElementById("step-results");
    if (stepsBox) stepsBox.addEventListener("ma:steps-rendered", () => renderAttempts(attemptBlocks));
    function renderAttempts(blocks) {
      attemptBlocks = blocks;
      for (const b of blocks || []) {
        const body = document.querySelector(`#step-results details.ui-step[data-step="${CSS.escape(b.module)}"] .ui-step-body`);
        if (!body) continue;
        const old = body.querySelector(":scope > .step-attempts");
        if (old) old.remove();
        if (!(b.earlier || []).length) continue;
        const box = histEl("div", "step-attempts");
        box.append(histEl("p", "step-attempts-title", histText("attempts_title")), histAttemptList(b));
        // Below the "Neu ausführen" row (rerun.js), else at the top
        const rerunRow = body.querySelector(":scope > .step-rerun");
        if (rerunRow) rerunRow.after(box);
        else body.prepend(box);
      }
    }

    async function refresh() {
      let data;
      try {
        data = await histFetchJob(debugKey);
      } catch (e) {
        if (last === null) {
          for (const box of [opsBox, copiesBox, labelsBox]) if (box) box.replaceChildren(histState(histText("load_failed"), true));
        }
        return;
      }
      // rerun.js keeps the "Neu ausführen" menu and buttons in step with every poll
      document.dispatchEvent(new CustomEvent("ma:job-history", {detail: data}));
      const sig = JSON.stringify([data.modules, data.intake, data.ops, data.copies, data.labels]);
      if (sig === last) return;
      last = sig;
      renderBar(data.modules, data.summary);
      renderAttempts(data.modules);
      if (opsBox) { histReplaceKeepOpen(opsBox, histOps(data.ops, data.intake)); opsBox.removeAttribute("aria-busy"); }
      if (labelsBox) { labelsBox.replaceChildren(...histLabels(data.labels)); labelsBox.removeAttribute("aria-busy"); }
      if (copiesBox) {
        const pastOpen = !!copiesBox.querySelector(".hist-copies-past[open]");
        copiesBox.replaceChildren(histCopies(data.copies, {restore}));
        const past = copiesBox.querySelector(".hist-copies-past");
        if (past && pastOpen) past.open = true;
      }
      if (opsCount) { opsCount.textContent = String(data.ops.length); opsCount.hidden = !data.ops.length; }
      if (copiesCount) { copiesCount.textContent = String(data.copies.length); copiesCount.hidden = !data.copies.length; }
    }

    refresh();
    setInterval(refresh, POLL_INTERVAL);
    // After a start from this page (rerun.js): show the new state right away
    document.addEventListener("ma:job-refresh", refresh);
  }

  // The job message with every other job's debug key linked to its page,
  // like the template filter link_job_keys (routers/logs.py). Rebuilt only
  // when the text changed, so a focused link survives the poll.
  function setJobMessage(el, text, ownKey) {
    if (el.dataset.raw === text) return;
    el.dataset.raw = text;
    el.replaceChildren();
    const re = /\bMA-\d{4}-\d+\b/g;
    let pos = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > pos) el.append(text.slice(pos, m.index));
      if (m[0] === ownKey) {
        el.append(m[0]);
      } else {
        const a = document.createElement("a");
        a.className = "ui-link";
        a.href = "/logs/job/" + encodeURIComponent(m[0]);
        a.textContent = m[0];
        el.append(a);
      }
      pos = m.index + m[0].length;
    }
    if (pos < text.length) el.append(text.slice(pos));
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
    // A dry run's planned target (IA-08 step result, rendered by the page) stays
    const targetEl = document.querySelector("[data-field='target_path']");
    if (targetEl && !(targetEl.dataset.planned && !job.target_path)) {
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
      setJobMessage(msgEl, job.error_message || "", job.debug_key);
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
    // IA-09 only mails about failed jobs; it shows up only when it sent
    // something or failed itself (same rule as job_view.step_visible).
    const entries = Object.entries(job.step_result || {}).filter(([step, r]) =>
      step !== "IA-09" || (r && typeof r === "object" && (r.sent || r.status === "error")));
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
      // The last inventory check found discrepancies: one quiet row below the modules
      const inv = data.inventory;
      if (inv && inv.href) {
        list.insertAdjacentHTML("beforeend", `<div class="ui-menu-sep" role="separator"></div>
          <a class="sm-row sm-misconfigured sm-inventory" href="${esc(inv.href)}">
            <span class="sm-dot" aria-hidden="true"></span>
            <span class="sm-text">
              <span class="sm-name">${esc(inv.label)}</span>
              <span class="sm-detail">${esc(inv.detail)}</span>
            </span>
          </a>`);
      }
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
  initHistoryTables();
  initJobDetail();
  initLightbox();
  initMenus();
  initQuickMenu();
  initStatusMenu();

  // For the pages' own scripts
  window.MA = {
    confirm: confirmAsk, confirmOptions,
    history: { text: histText, el: histEl, iconEl: histIconEl, badge: histBadge, state: histState,
               restore: histRestore, alert: histAlert },
  };
})();
