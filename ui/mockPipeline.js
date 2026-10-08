/**
 * In-memory concurrent remediating jobs. Local demo only — no AWS.
 *
 * Skip Autotag when the filename contains "skip" (case-insensitive),
 * otherwise Autotag runs in the mock.
 */

const jobs = new Map();
const listeners = new Set();

const STAGES = [
  { id: "upload", label: "Uploading", percent: 10, ms: 500 },
  { id: "split", label: "Splitting", percent: 22, ms: 400 },
  { id: "verapdf", label: "veraPDF gate", percent: 38, ms: 700 },
  { id: "autotag", label: "Adobe Autotag", percent: 58, ms: 900 },
  { id: "alttext", label: "Alt text", percent: 72, ms: 600 },
  { id: "title", label: "Title / merge", percent: 84, ms: 450 },
  { id: "postcheck", label: "Post-check", percent: 94, ms: 500 },
  { id: "done", label: "Done", percent: 100, ms: 200 },
];

function notify() {
  const snapshot = [...jobs.values()].map((job) => ({ ...job }));
  listeners.forEach((fn) => fn(snapshot));
}

function shouldSkipAutotag(filename) {
  return /skip/i.test(filename);
}

function buildCategories(skipAutotag) {
  return [
    { name: "Document", passed: skipAutotag ? 5 : 6, failed: skipAutotag ? 1 : 0, needs_manual_check: 2 },
    { name: "Page Content", passed: skipAutotag ? 7 : 8, failed: skipAutotag ? 2 : 1, needs_manual_check: 0 },
    { name: "Forms", passed: 2, failed: 0, needs_manual_check: 0 },
    { name: "Alternate Text", passed: 5, failed: 0, needs_manual_check: 0 },
    { name: "Tables", passed: 5, failed: 0, needs_manual_check: 0 },
    { name: "Lists", passed: 2, failed: 0, needs_manual_check: 0 },
    { name: "Headings", passed: 1, failed: 0, needs_manual_check: 0 },
  ];
}

function sumCategoryCounts(categories) {
  return categories.reduce(
    (acc, row) => ({
      passed: acc.passed + row.passed,
      failed: acc.failed + row.failed,
      needs_manual_check: acc.needs_manual_check + row.needs_manual_check,
    }),
    { passed: 0, failed: 0, needs_manual_check: 0 },
  );
}

function detailedReportFromCategories(categories) {
  const report = {};
  for (const row of categories) {
    const rules = [];
    for (let i = 0; i < row.passed; i += 1) {
      rules.push({ Rule: `${row.name} ${i + 1}`, Status: "Passed", Description: `${row.name} check passed` });
    }
    for (let i = 0; i < row.failed; i += 1) {
      rules.push({ Rule: `${row.name} failed ${i + 1}`, Status: "Failed", Description: `${row.name} check failed` });
    }
    for (let i = 0; i < row.needs_manual_check; i += 1) {
      rules.push({ Rule: `${row.name} manual ${i + 1}`, Status: "Needs manual check", Description: `${row.name} needs a manual check` });
    }
    report[row.name] = rules;
  }
  return report;
}

function stem(filename) {
  return filename.replace(/\.pdf$/i, "");
}

function buildReports(filename, skipAutotag, categories, veraPDF, remediation) {
  const source = stem(filename);
  const compliant = source.startsWith("COMPLIANT_") ? source : `COMPLIANT_${source}`;
  const veraSummary = {
    tool: "veraPDF",
    flavour: "ua1",
    source_pdf: `COMPLIANT_${filename}`,
    ...veraPDF,
    passed_rules: skipAutotag ? 80 : 75,
    passed_checks: skipAutotag ? 400 : 380,
    failed_checks: skipAutotag ? 4 : 8,
    tagging_failed_rules: skipAutotag
      ? []
      : [{ specification: "ISO 14289-1:2014", clause: "7.1", description: "All real content shall be tagged", failed_checks: 3, status: "failed" }],
    other_failed_rules: [
      { specification: "ISO 14289-1:2014", clause: "7.3", description: "Figures shall have alternate text", failed_checks: 2, status: "failed" },
    ],
  };
  const { tagging_failed_rules, other_failed_rules, ...summaryWithoutRawLists } = veraSummary;
  return {
    afterReport: {
      filename: `${compliant}_accessibility_report_after_remidiation.json`,
      body: {
        Summary: {
          Description: "The checker found problems which may prevent the document from being fully accessible.",
          "Needs manual check": remediation.needs_manual_check,
          "Passed manually": 0,
          "Failed manually": 0,
          Skipped: 0,
          Passed: remediation.passed,
          Failed: remediation.failed,
        },
        "Detailed Report": detailedReportFromCategories(categories),
      },
    },
    remediationStats: {
      filename: `${compliant}_remediation_stats.json`,
      body: {
        filename: `${compliant}.pdf`,
        adobe_calls_this_step: 1,
        adobe_postcheck: remediation.adobe_postcheck,
        adobe_precheck: 0,
        adobe_extract: 0,
        adobe_autotag: remediation.adobe_autotag,
        adobe_jobs_per_pdf: skipAutotag ? 1 : 2,
        report_s3: `temp/${source}/accessability-report/${compliant}_accessibility_report_after_remidiation.json`,
        passed: remediation.passed,
        failed: remediation.failed,
        needs_manual_check: remediation.needs_manual_check,
      },
    },
    verapdfReport: {
      filename: `${filename.replace(/\.pdf$/i, "")}_verapdf_report.json`,
      body: {
        report: {
          jobs: [
            {
              validationResult: {
                compliant: veraPDF.is_compliant,
                statement: veraPDF.is_compliant
                  ? "PDF file is compliant with Validation Profile requirements."
                  : "PDF file is not compliant with Validation Profile requirements.",
                profileName: "PDF/UA-1 validation profile",
                details: {
                  passedRules: veraSummary.passed_rules,
                  failedRules: veraPDF.failed_rules,
                  passedChecks: veraSummary.passed_checks,
                  failedChecks: veraSummary.failed_checks,
                  ruleSummaries: [
                    ...veraSummary.tagging_failed_rules,
                    ...veraSummary.other_failed_rules,
                  ],
                },
              },
            },
          ],
        },
      },
    },
    verapdfSummary: {
      filename: `${filename.replace(/\.pdf$/i, "")}_verapdf_summary.json`,
      body: {
        ...summaryWithoutRawLists,
        tagging_failed_rules,
        other_failed_rules,
        s3_key: `temp/${source}/accessability-report/${filename.replace(/\.pdf$/i, "")}_verapdf_summary.json`,
      },
    },
  };
}

function buildStats(filename, skipAutotag) {
  const failedRules = skipAutotag ? 4 : 5;
  const categories = buildCategories(skipAutotag);
  const totals = sumCategoryCounts(categories);
  const veraPDF = {
    is_compliant: false,
    has_structure_tree: skipAutotag,
    failed_rules: failedRules,
    tagging_failures: skipAutotag ? 0 : 1,
    other_failures: 4,
    call_adobe_autotag: !skipAutotag,
    decision_reason: skipAutotag
      ? "Already tagged; remaining veraPDF failures are not Autotag work (alt text, title, language, contrast, etc.)"
      : "No PDF structure tree (StructTreeRoot); Adobe Autotag is required",
  };
  const remediation = {
    adobe_autotag: skipAutotag ? 0 : 1,
    adobe_autotag_skipped: skipAutotag ? 1 : 0,
    adobe_postcheck: 1,
    passed: totals.passed,
    failed: totals.failed,
    needs_manual_check: totals.needs_manual_check,
    images_extracted: skipAutotag ? 0 : 4,
    toc_entries: 2,
  };
  return {
    veraPDF,
    remediation,
    categories,
    reports: buildReports(filename, skipAutotag, categories, veraPDF, remediation),
  };
}

export function subscribe(fn) {
  listeners.add(fn);
  fn([...jobs.values()].map((job) => ({ ...job })));
  return () => listeners.delete(fn);
}

export function getJob(id) {
  const job = jobs.get(id);
  return job ? { ...job } : null;
}

export function queueFile(file) {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const skipAutotag = shouldSkipAutotag(file.name);
  jobs.set(id, {
    id,
    name: file.name,
    size: file.size,
    status: "ready",
    percent: 0,
    step: "Ready",
    skipAutotag,
    stats: null,
    originalBlob: file,
    error: null,
    startedAt: null,
    finishedAt: null,
  });
  notify();
  return id;
}

export function startQueuedJob(id) {
  const job = jobs.get(id);
  if (!job || job.status !== "ready") return;
  job.status = "running";
  job.step = "Starting";
  job.startedAt = Date.now();
  notify();
  runStages(id).catch((err) => {
    const failed = jobs.get(id);
    if (!failed) return;
    failed.status = "failed";
    failed.step = "Failed";
    failed.error = err.message || String(err);
    failed.finishedAt = Date.now();
    notify();
  });
}

export function startJob(file) {
  const id = queueFile(file);
  startQueuedJob(id);
  return id;
}

async function runStages(id) {
  const job = jobs.get(id);
  if (!job) return;

  for (const stage of STAGES) {
    const current = jobs.get(id);
    if (!current) return;

    if (stage.id === "autotag" && current.skipAutotag) {
      current.status = "running";
      current.step = "Skipping Adobe Autotag";
      current.percent = stage.percent;
      notify();
      await wait(350);
      continue;
    }

    current.status = stage.id === "done" ? "done" : "running";
    current.step = stage.label;
    current.percent = stage.percent;
    if (stage.id === "verapdf" || stage.id === "done") {
      current.stats = buildStats(current.name, current.skipAutotag);
    }
    if (stage.id === "done") {
      current.finishedAt = Date.now();
    }
    notify();
    await wait(stage.ms);
  }
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
