import { queueFile, startQueuedJob, subscribe } from "./mockPipeline.js";
import {
  apiList,
  apiLogin,
  apiStart,
  apiStatus,
  apiUploadUrl,
  clearToken,
  downloadS3,
  isLive,
  putPdf,
} from "./api.js";

const SESSION_KEY = "pdf-remediation-demo-auth";

const loginScreen = document.getElementById("login-screen");
const appScreen = document.getElementById("app-screen");
const loginForm = document.getElementById("login-form");
const loginError = document.getElementById("login-error");
const logoutBtn = document.getElementById("logout");
const fileListEl = document.getElementById("file-list");
const fileInput = document.getElementById("file-input");
const dropzone = document.getElementById("dropzone");
const detailEl = document.getElementById("detail");

let jobs = [];
let selectedId = null;
let pollTimer = null;

function isAuthed() {
  return sessionStorage.getItem(SESSION_KEY) === "1";
}

function showApp(authed) {
  loginScreen.classList.toggle("hidden", authed);
  appScreen.classList.toggle("hidden", !authed);
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const user = document.getElementById("username").value.trim();
  const pass = document.getElementById("password").value;
  loginError.classList.add("hidden");
  try {
    if (isLive()) {
      await apiLogin(user, pass);
    } else if (!user.toLowerCase().endsWith("@sparient.com") || pass !== "sparient123") {
      throw new Error("Use your @sparient.com email and password.");
    }
    sessionStorage.setItem(SESSION_KEY, "1");
    showApp(true);
    if (isLive()) await refreshLiveList();
  } catch (err) {
    loginError.textContent = err.message || "Use your @sparient.com email and password.";
    loginError.classList.remove("hidden");
  }
});

logoutBtn.addEventListener("click", () => {
  sessionStorage.removeItem(SESSION_KEY);
  clearToken();
  selectedId = null;
  jobs = [];
  showApp(false);
  render();
});

function liveJobFromStatus(item) {
  return {
    id: item.file,
    name: item.file,
    status: item.status,
    percent: item.percent,
    step: item.step,
    stats: { categories: item.categories || [], reports: item.reports || {} },
    originalKey: item.original_key,
    resultKey: item.result_key,
    error: item.error || null,
  };
}

async function refreshLiveList() {
  const data = await apiList();
  jobs = (data.files || []).map(liveJobFromStatus);
  if (selectedId && !jobs.some((job) => job.id === selectedId)) selectedId = jobs[0]?.id || null;
  if (!selectedId && jobs[0]) selectedId = jobs[0].id;
  render();
  startPolling();
}

async function addFiles(fileList) {
  for (const file of [...fileList]) {
    if (!file.name.toLowerCase().endsWith(".pdf")) continue;
    if (isLive()) {
      const { url, file: name } = await apiUploadUrl(file.name);
      await putPdf(url, file);
      selectedId = name;
      await refreshLiveList();
    } else {
      selectedId = queueFile(file);
    }
  }
}

fileInput.addEventListener("change", async () => {
  try {
    await addFiles(fileInput.files);
  } catch (err) {
    window.alert(err.message);
  }
  fileInput.value = "";
});

["dragenter", "dragover"].forEach((type) => {
  dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.classList.add("drag");
  });
});

["dragleave", "drop"].forEach((type) => {
  dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.classList.remove("drag");
  });
});

dropzone.addEventListener("drop", async (event) => {
  try {
    await addFiles(event.dataTransfer.files);
  } catch (err) {
    window.alert(err.message);
  }
});

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function startSelected(job) {
  try {
    if (isLive()) {
      await apiStart(job.name);
      await refreshLiveList();
    } else {
      startQueuedJob(job.id);
    }
  } catch (err) {
    window.alert(err.message);
  }
}

async function downloadKey(key) {
  if (!key) return;
  try {
    await downloadS3(key);
  } catch (err) {
    window.alert(err.message);
  }
}

function statusLabel(status) {
  const labels = {
    ready: "Ready",
    queued: "Queued",
    running: "Running",
    done: "Completed",
    failed: "Failed",
    missing: "Not found",
  };
  return labels[status] || status || "Ready";
}

function renderList() {
  if (!jobs.length) {
    fileListEl.innerHTML = '<p class="muted">No files yet.</p>';
    return;
  }
  fileListEl.innerHTML = [...jobs].reverse().map((job) => `
    <button class="file-item ${job.id === selectedId ? "active" : ""}" data-id="${job.id}" type="button">
      <span class="file-name">${escapeHtml(job.name)}</span>
      <span class="chip ${job.status}">${escapeHtml(statusLabel(job.status))}</span>
    </button>
  `).join("");
  fileListEl.querySelectorAll(".file-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      selectedId = btn.dataset.id;
      render();
    });
  });
}

function categoryPills(row) {
  return `
    <span class="category-pills">
      <span class="pill passed">Passed ${row.passed}</span>
      <span class="pill failed">Failed ${row.failed}</span>
      <span class="pill manual">Needs manual ${row.needs_manual_check}</span>
    </span>
  `;
}

function renderDetail() {
  const job = jobs.find((item) => item.id === selectedId);
  if (!job) {
    detailEl.innerHTML = '<p class="muted">Select a file on the left to see stats and downloads.</p>';
    return;
  }

  const reports = job.stats?.reports || {};
  const reportsReady = job.status === "done";
  const canStart = job.status === "ready" || job.status === "queued";
  const categories = job.stats?.categories || [];
  const totals = categories.reduce(
    (acc, row) => ({
      passed: acc.passed + row.passed,
      failed: acc.failed + row.failed,
      needs_manual_check: acc.needs_manual_check + row.needs_manual_check,
    }),
    { passed: 0, failed: 0, needs_manual_check: 0 },
  );
  const originalReady = Boolean(job.originalBlob || job.originalKey);
  const remediatingReady = job.status === "done" && Boolean(job.originalBlob || job.resultKey);

  detailEl.innerHTML = `
    <h2>${escapeHtml(job.name)}</h2>
    <p class="status-line"><span class="chip ${job.status}">${escapeHtml(statusLabel(job.status))}</span></p>
    ${job.error ? `<p class="error">${escapeHtml(job.error)}</p>` : ""}
    <div class="btn-row">
      <button class="btn" type="button" id="start-remediation" ${canStart ? "" : "disabled"}>Start remediating</button>
      <button class="btn secondary" type="button" id="dl-original" ${originalReady ? "" : "disabled"}>Download original</button>
      <button class="btn secondary" type="button" id="dl-remediated" ${remediatingReady ? "" : "disabled"}>Download remediated</button>
    </div>
    ${categories.length ? `
      <h2 style="margin-top:20px">Issues by category</h2>
      <p class="muted">From the most recent Adobe accessibility check for this file.</p>
      <div class="category-list">
        ${categories.map((row) => `
          <div class="category-row">
            <span class="category-name">${escapeHtml(row.name)}</span>
            ${categoryPills(row)}
          </div>
        `).join("")}
        <div class="category-row total">
          <span class="category-name">Total</span>
          ${categoryPills(totals)}
        </div>
      </div>
    ` : `<p class="muted" style="margin-top:16px">${canStart ? "Upload saved. Click Start remediating." : "Category results appear after the accessibility check."}</p>`}
    <h2 style="margin-top:20px">Reports</h2>
    <div class="btn-row">
      <button class="btn secondary" type="button" id="dl-after-report" ${reportsReady && reports.afterReport ? "" : "disabled"}>Report after remediation</button>
      <button class="btn secondary" type="button" id="dl-remediation-stats" ${reportsReady && reports.remediationStats ? "" : "disabled"}>Remediation stats</button>
      <button class="btn secondary" type="button" id="dl-verapdf-report" ${reportsReady && reports.verapdfReport ? "" : "disabled"}>veraPDF report</button>
      <button class="btn secondary" type="button" id="dl-verapdf-summary" ${reportsReady && reports.verapdfSummary ? "" : "disabled"}>veraPDF summary</button>
    </div>
  `;

  document.getElementById("start-remediation")?.addEventListener("click", () => startSelected(job));
  document.getElementById("dl-original")?.addEventListener("click", () => {
    if (job.originalBlob) downloadBlob(job.originalBlob, job.name);
    else downloadKey(job.originalKey);
  });
  document.getElementById("dl-remediated")?.addEventListener("click", () => {
    if (job.originalBlob && !isLive()) downloadBlob(job.originalBlob, `COMPLIANT_${job.name}`);
    else downloadKey(job.resultKey);
  });
  document.getElementById("dl-after-report")?.addEventListener("click", () => {
    if (isLive()) downloadKey(reports.afterReport);
    else downloadMockReport(job, "afterReport");
  });
  document.getElementById("dl-remediation-stats")?.addEventListener("click", () => {
    if (isLive()) downloadKey(reports.remediationStats);
    else downloadMockReport(job, "remediationStats");
  });
  document.getElementById("dl-verapdf-report")?.addEventListener("click", () => {
    if (isLive()) downloadKey(reports.verapdfReport);
    else downloadMockReport(job, "verapdfReport");
  });
  document.getElementById("dl-verapdf-summary")?.addEventListener("click", () => {
    if (isLive()) downloadKey(reports.verapdfSummary);
    else downloadMockReport(job, "verapdfSummary");
  });
}

function downloadMockReport(job, key) {
  const report = job.stats?.reports?.[key];
  if (!report?.body) return;
  const blob = new Blob([JSON.stringify(report.body, null, 2)], { type: "application/json" });
  downloadBlob(blob, report.filename);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function render() {
  renderList();
  renderDetail();
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  if (!isLive() || !isAuthed()) return;
  pollTimer = setInterval(async () => {
    const running = jobs.filter((job) => job.status === "running");
    if (!running.length) return;
    try {
      const updates = await Promise.all(running.map((job) => apiStatus(job.name)));
      updates.forEach((item) => {
        const idx = jobs.findIndex((job) => job.name === item.file);
        if (idx >= 0) jobs[idx] = liveJobFromStatus(item);
      });
      render();
    } catch {
      /* keep last known status */
    }
  }, 4000);
}

subscribe((next) => {
  if (isLive()) return;
  jobs = next;
  if (selectedId && !jobs.some((job) => job.id === selectedId)) {
    selectedId = jobs[0]?.id || null;
  }
  if (!selectedId && jobs[0]) selectedId = jobs[0].id;
  render();
});

showApp(isAuthed());
if (isAuthed() && isLive()) {
  refreshLiveList().catch(() => {
    sessionStorage.removeItem(SESSION_KEY);
    clearToken();
    showApp(false);
  });
}
