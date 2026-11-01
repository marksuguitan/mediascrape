(() => {
  const $ = (id) => document.getElementById(id);
  const form = $("form");
  const submit = $("submit");
  const statusBox = $("status");
  const statusText = $("status-text");
  const bar = $("bar");
  const barFill = $("bar-fill");
  const result = $("result");
  const codeField = $("code-field");
  const codeInput = $("code");

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const LABELS = {
    queued: "Queued…",
    starting: "Talking to YouTube…",
    downloading: "Downloading…",
    uploading: "Almost done, preparing your file…",
  };

  function show(text, { error = false, percent = null } = {}) {
    statusBox.hidden = false;
    statusBox.classList.toggle("error", error);
    statusText.textContent = text;
    bar.hidden = percent === null;
    if (percent !== null) barFill.style.width = `${Math.max(2, Math.min(100, percent))}%`;
  }

  function formatSize(bytes) {
    if (!bytes) return "";
    const mb = bytes / (1024 * 1024);
    return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
  }

  async function api(path, options) {
    const res = await fetch(`/api/${path}`, options);
    let data = {};
    try { data = await res.json(); } catch { /* non-JSON error page */ }
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status}).`);
    return data;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function poll(jobId) {
    const started = Date.now();
    while (Date.now() - started < 16 * 60 * 1000) {
      await sleep(2000);
      let s;
      try {
        s = await api(`status?id=${encodeURIComponent(jobId)}`);
      } catch (e) {
        continue; // transient; keep polling
      }
      if (s.state === "done") return s;
      if (s.state === "error") throw new Error(s.error || "Download failed.");
      const pct = typeof s.percent === "number" ? s.percent : null;
      const label = LABELS[s.state] || "Working…";
      show(pct !== null ? `${label} ${pct.toFixed(0)}%` : label, {
        percent: s.state === "uploading" ? 100 : pct,
      });
    }
    throw new Error("This is taking too long. Try a lower quality.");
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    result.hidden = true;
    submit.disabled = true;
    show("Starting…");
    try {
      const body = {
        url: $("url").value,
        quality: $("quality").value,
        code: codeInput.value || undefined,
      };
      const { jobId } = await api("download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (codeInput.value) store.set("mediascrape.code", codeInput.value);
      const done = await poll(jobId);
      show(`Ready: ${done.title}`);
      result.href = done.file;
      result.setAttribute("download", done.filename || "");
      result.textContent = `Save ${done.filename}${done.size ? ` (${formatSize(done.size)})` : ""}`;
      result.hidden = false;
      result.click();
    } catch (err) {
      show(err.message || String(err), { error: true });
    } finally {
      submit.disabled = false;
    }
  });

  // Show the access-code box only when the backend asks for one.
  api("config")
    .then((cfg) => {
      if (cfg.codeRequired) {
        codeField.hidden = false;
        codeInput.required = true;
        codeInput.value = store.get("mediascrape.code") || "";
      }
    })
    .catch(() => { /* backend not deployed yet; the form still works */ });
})();
