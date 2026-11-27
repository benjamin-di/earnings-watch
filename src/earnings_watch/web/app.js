/* Same-origin progressive enhancement for Earnings Watch. Tokens stay server-side. */
"use strict";
(() => {
  // 0. Close the Apps switcher on outside click or Escape.
  const switcher = document.querySelector("details.suite-switch");
  if (switcher) {
    document.addEventListener("click", (event) => {
      if (switcher.open && !switcher.contains(event.target)) {
        switcher.open = false;
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !switcher.open) return;
      switcher.open = false;
      switcher.querySelector("summary")?.focus();
    });
  }

  const statusBanner = document.getElementById("status-banner");

  function setStatus(text, kind = "ok") {
    if (!statusBanner) return;
    statusBanner.textContent = text || "";
    statusBanner.classList.remove("ok", "error");
    statusBanner.hidden = !text;
    if (text) statusBanner.classList.add(kind);
  }

  function syncRevisions(nextRevision) {
    if (nextRevision === undefined || nextRevision === null) return;
    const value = String(nextRevision);
    for (const input of document.querySelectorAll('input[name="revision"]')) {
      input.value = value;
    }
  }

  // 1. Copy private calendar link or suite address with clear feedback.
  document.addEventListener("click", async (event) => {
    const btn = event.target.closest("button[data-copy-code]");
    if (!btn) return;
    const target = document.getElementById(btn.dataset.copyCode);
    if (!target) return;
    const text = (target.textContent || "").trim();
    if (!text) return;
    const original = btn.dataset.originalLabel || btn.textContent;
    btn.dataset.originalLabel = original;
    const copiedLabel = btn.dataset.copied || "Link copied";
    try {
      await navigator.clipboard.writeText(text);
      btn.textContent = copiedLabel;
      setTimeout(() => {
        if (btn.textContent === copiedLabel) btn.textContent = original;
      }, 2200);
    } catch (_) {
      const range = document.createRange();
      range.selectNodeContents(target);
      const sel = window.getSelection();
      sel?.removeAllRanges();
      sel?.addRange(range);
      btn.textContent = "Selected — copy manually";
    }
  });

  // 2. Instant client-side search & filter on Companies tab.
  const searchInput = document.getElementById("company-search");
  const filterSelect = document.getElementById("company-filter");
  const countLabel = document.getElementById("company-count");
  const noMatch = document.getElementById("company-no-match");
  const companyRows = Array.from(document.querySelectorAll(".company-row"));

  function applyCompanyFilter() {
    const query = (searchInput?.value || "").trim().toLowerCase();
    const filter = filterSelect?.value || "all";
    let visible = 0;
    for (const row of companyRows) {
      const haystack = (row.dataset.search || "").toLowerCase();
      const isHolding = row.dataset.holding === "true";
      const isWatchlist = row.dataset.watchlist === "true";
      const isMuted =
        row.dataset.mutedCal === "true" || row.dataset.mutedNews === "true";
      let matchesFilter = true;
      if (filter === "holding") matchesFilter = isHolding;
      else if (filter === "watchlist") matchesFilter = isWatchlist;
      else if (filter === "muted") matchesFilter = isMuted;
      const show = matchesFilter && (!query || haystack.includes(query));
      row.hidden = !show;
      if (show) visible += 1;
    }
    if (countLabel) {
      countLabel.textContent =
        visible === companyRows.length
          ? `${visible} companies`
          : `${visible} of ${companyRows.length} companies`;
    }
    if (noMatch) noMatch.hidden = companyRows.length === 0 || visible > 0;
  }

  searchInput?.addEventListener("input", applyCompanyFilter);
  filterSelect?.addEventListener("change", applyCompanyFilter);

  // 3. Progressive-enhancement async mutations serialized through a single-flight queue
  // so rapid consecutive clicks always read the latest synced revision.
  let submitQueue = Promise.resolve();

  async function runAsyncSubmit(form, submitBtn, mode) {
    const errorBox = form.querySelector(".form-error");
    if (errorBox) errorBox.textContent = "";
    try {
      const response = await fetch(form.action, {
        method: (form.method || "POST").toUpperCase(),
        body: new FormData(form),
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        cache: "no-store",
      });
      const contentType = response.headers.get("content-type") || "";
      if (!contentType.includes("application/json")) {
        window.location.reload();
        return;
      }
      const data = await response.json();
      if (data.revision !== undefined) syncRevisions(data.revision);

      if (!response.ok || !data.ok) {
        const msg = data.error || "Could not save right now. Refresh and try again.";
        if (errorBox) errorBox.textContent = msg;
        setStatus(msg, "error");
        return;
      }

      if (data.message) setStatus(data.message, "ok");
      if (data.reload || mode === "reload") {
        window.location.reload();
        return;
      }

      if (mode === "toggle" && data.next_action && submitBtn) {
        form.action = `/connections/${data.next_action}`;
        const exchInput = form.querySelector('input[name="exchange"]');
        if (exchInput && data.next_exchange !== undefined) {
          exchInput.value = data.next_exchange;
        }
        const muted = Boolean(data.muted);
        submitBtn.textContent = data.next_label || (muted ? "Off" : "On");
        submitBtn.classList.toggle("toggle-on", !muted);
        submitBtn.classList.toggle("toggle-off", muted);
        const row = form.closest(".company-row");
        if (row) {
          if (data.channel === "calendar") row.dataset.mutedCal = String(muted);
          if (data.channel === "news") row.dataset.mutedNews = String(muted);
          applyCompanyFilter();
        }
      } else if (mode === "remove") {
        const row = form.closest(".company-row");
        if (row && row.dataset.holding === "true") {
          row.dataset.watchlist = "false";
          row.querySelector('[data-badge="watchlist"]')?.remove();
          form.remove();
          applyCompanyFilter();
        } else if (row) {
          const idx = companyRows.indexOf(row);
          if (idx >= 0) companyRows.splice(idx, 1);
          row.remove();
          applyCompanyFilter();
        }
      } else if (mode === "mark-seen") {
        for (const badge of document.querySelectorAll('[data-badge="changed"]')) {
          badge.remove();
        }
        form.remove();
      }
    } catch (_) {
      setStatus("Could not reach the server. Refresh and try again.", "error");
    } finally {
      if (submitBtn && submitBtn.isConnected) submitBtn.disabled = false;
    }
  }

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form");
    if (!form) return;
    const submitBtn = form.querySelector('button[type="submit"]');
    const mode = form.dataset.async || submitBtn?.dataset.asyncBtn;
    if (!mode) return;
    event.preventDefault();
    if (submitBtn) submitBtn.disabled = true;
    submitQueue = submitQueue.then(() => runAsyncSubmit(form, submitBtn, mode));
  });
})();
