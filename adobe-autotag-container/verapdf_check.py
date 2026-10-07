"""
Run veraPDF PDF/UA-1 before Adobe Autotag.

veraPDF is an open-source validator. It does not tag the PDF and does not
replace Autotag. It tells us whether the file already has a usable tag tree
so we can skip a billed Adobe Autotag job.

Decision:
- Call Adobe Autotag if there is no structure tree, or veraPDF fails
  tagging-related rules (untagged content / missing StructTreeRoot).
- Skip Autotag if PDF/UA-1 passes, or the file is already tagged and the
  remaining failures are not tagging (alt text, title, language, contrast).
- If veraPDF cannot run, call Autotag (safe default).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pymupdf

logger = logging.getLogger(__name__)

FLAVOUR = "ua1"
DEFAULT_TIMEOUT_SEC = 240

TAGGING_HINTS = (
    "structtreeroot",
    "struct tree",
    "structure tree",
    "untagged",
    "not tagged",
    "shall be tagged",
    "must be tagged",
    "content is not tagged",
    "missing tag",
    "logical structure",
)

FIELDS_EXPLAINED = {
    "tool": "Always veraPDF.",
    "flavour": "Validation profile. ua1 = PDF/UA-1 (ISO 14289-1).",
    "is_compliant": "true only if the whole PDF/UA-1 profile passed.",
    "statement": "veraPDF one-line result (compliant / not compliant).",
    "profile_name": "Name of the built-in profile that was applied.",
    "passed_rules": "Count of UA rules that passed.",
    "failed_rules": "Count of UA rules that failed.",
    "passed_checks": "Count of individual checks that passed.",
    "failed_checks": "Count of individual checks that failed.",
    "failed_rule_details": "Failed rules: specification, clause, description, failedChecks.",
    "tagging_failed_rules": "Subset of failures about missing/untagged structure.",
    "other_failed_rules": "Other UA failures (alt text, title, language, contrast, etc.).",
    "has_structure_tree": "PDF catalog has StructTreeRoot (already tagged at all).",
    "call_adobe_autotag": "true = this container must call Adobe Autotag.",
    "decision_reason": "Why Autotag is called or skipped.",
    "error": "Set only when veraPDF itself failed to run.",
}


def _local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def has_structure_tree(pdf_path: str) -> bool:
    doc = pymupdf.open(pdf_path)
    try:
        catalog = doc.pdf_catalog()
        key_type, _value = doc.xref_get_key(catalog, "StructTreeRoot")
        return key_type not in ("null", "none", "")
    except Exception as exc:
        logger.warning("Could not read StructTreeRoot from %s: %s", pdf_path, exc)
        return False
    finally:
        doc.close()


def _is_tagging_rule(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in TAGGING_HINTS)


def _rule_blob(rule: dict) -> str:
    return " ".join(
        str(rule.get(key) or "")
        for key in ("specification", "clause", "description", "object", "test")
    )


def _parse_json_report(raw: str) -> dict:
    payload = json.loads(raw)
    report = payload.get("report") or payload
    jobs = report.get("jobs") or []
    job = jobs[0] if jobs else {}
    result = (
        job.get("validationResult")
        or job.get("validationResults")
        or job.get("validationReport")
        or {}
    )
    if isinstance(result, list):
        result = result[0] if result else {}
    details = result.get("details") or {}
    summaries = details.get("ruleSummaries") or details.get("rules") or []
    failed = []
    for item in summaries:
        status = str(item.get("status") or item.get("ruleStatus") or "").lower()
        failed_checks = item.get("failedChecks")
        if status not in ("failed", "fail") and not failed_checks:
            continue
        failed.append({
            "specification": item.get("specification") or item.get("specificationName"),
            "clause": item.get("clause"),
            "test_number": item.get("testNumber"),
            "description": item.get("description") or item.get("ruleDescription"),
            "failed_checks": failed_checks if failed_checks is not None else item.get("failedChecksCount"),
            "status": status or "failed",
        })
    return {
        "is_compliant": bool(result.get("compliant") or result.get("isCompliant")),
        "statement": result.get("statement") or "",
        "profile_name": result.get("profileName") or result.get("profile") or "PDF/UA-1",
        "passed_rules": details.get("passedRules"),
        "failed_rules": details.get("failedRules", len(failed)),
        "passed_checks": details.get("passedChecks"),
        "failed_checks": details.get("failedChecks"),
        "failed_rule_details": failed[:40],
    }


def _parse_xml_report(raw: str) -> dict:
    root = ET.fromstring(raw)
    report = None
    for node in root.iter():
        if _local_tag(node.tag) == "validationReport":
            report = node
            break
    if report is None:
        raise ValueError("veraPDF XML has no validationReport")
    details = None
    for child in report:
        if _local_tag(child.tag) == "details":
            details = child
            break
    failed = []
    if details is not None:
        for rule in details:
            if _local_tag(rule.tag) != "rule":
                continue
            if (rule.get("status") or "").lower() != "failed":
                continue
            description = ""
            for child in rule:
                if _local_tag(child.tag) == "description":
                    description = (child.text or "").strip()
                    break
            failed.append({
                "specification": rule.get("specification"),
                "clause": rule.get("clause"),
                "test_number": rule.get("testNumber"),
                "description": description,
                "failed_checks": rule.get("failedChecks"),
                "status": "failed",
            })
    compliant_attr = (report.get("isCompliant") or "").lower() == "true"
    return {
        "is_compliant": compliant_attr,
        "statement": report.get("statement") or "",
        "profile_name": report.get("profileName") or "PDF/UA-1",
        "passed_rules": None if details is None else details.get("passedRules"),
        "failed_rules": None if details is None else details.get("failedRules", str(len(failed))),
        "passed_checks": None if details is None else details.get("passedChecks"),
        "failed_checks": None if details is None else details.get("failedChecks"),
        "failed_rule_details": failed[:40],
    }


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def decide_call_adobe(parsed: dict, has_tags: bool) -> tuple[bool, str]:
    if parsed.get("error"):
        return True, "veraPDF did not run; calling Adobe Autotag as fallback"
    if parsed.get("is_compliant"):
        return False, "PDF/UA-1 compliant; Adobe Autotag is not needed"
    if not has_tags:
        return True, "No PDF structure tree (StructTreeRoot); Adobe Autotag is required"
    tagging = parsed.get("tagging_failed_rules") or []
    if tagging:
        return True, (
            "File has tags but veraPDF failed tagging rules "
            f"({len(tagging)}); Adobe Autotag is required"
        )
    return False, (
        "Already tagged; remaining veraPDF failures are not Autotag work "
        "(alt text, title, language, contrast, etc.)"
    )


def run_verapdf(pdf_path: str, flavour: str = FLAVOUR, timeout: int = DEFAULT_TIMEOUT_SEC) -> dict:
    """
    Validate pdf_path with veraPDF and return a decision plus report fields.
    """
    tagged = has_structure_tree(pdf_path)
    binary = os.environ.get("VERAPDF_BIN") or shutil.which("verapdf") or "/opt/verapdf/verapdf"
    summary = {
        "tool": "veraPDF",
        "flavour": flavour,
        "source_pdf": os.path.basename(pdf_path),
        "has_structure_tree": tagged,
        "fields_explained": FIELDS_EXPLAINED,
    }

    if not os.path.isfile(binary) and not shutil.which(binary):
        summary["error"] = f"veraPDF binary not found at {binary}"
        call_adobe, reason = decide_call_adobe(summary, tagged)
        summary["call_adobe_autotag"] = call_adobe
        summary["decision_reason"] = reason
        return summary

    cmd = [
        binary,
        "--flavour", flavour,
        "--format", "json",
        "--maxfailuresdisplayed", "40",
        pdf_path,
    ]
    logger.info("Running veraPDF: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        summary["error"] = f"veraPDF timed out after {timeout}s"
        call_adobe, reason = decide_call_adobe(summary, tagged)
        summary["call_adobe_autotag"] = call_adobe
        summary["decision_reason"] = reason
        return summary
    except OSError as exc:
        summary["error"] = f"veraPDF failed to start: {exc}"
        call_adobe, reason = decide_call_adobe(summary, tagged)
        summary["call_adobe_autotag"] = call_adobe
        summary["decision_reason"] = reason
        return summary

    raw = (proc.stdout or "").strip()
    summary["verapdf_exit_code"] = proc.returncode
    if proc.stderr:
        summary["verapdf_stderr"] = proc.stderr.strip()[:2000]

    parsed = {}
    parse_error = None
    if raw:
        try:
            parsed = _parse_json_report(raw)
        except Exception as json_exc:
            parse_error = f"json: {json_exc}"
            try:
                parsed = _parse_xml_report(raw)
                parse_error = None
            except Exception as xml_exc:
                parse_error = f"{parse_error}; xml: {xml_exc}"
    else:
        parse_error = proc.stderr.strip()[:500] or "veraPDF produced no stdout"

    if parse_error and not parsed:
        summary["error"] = parse_error
        summary["raw_report"] = raw[:4000]
        call_adobe, reason = decide_call_adobe(summary, tagged)
        summary["call_adobe_autotag"] = call_adobe
        summary["decision_reason"] = reason
        return summary

    for key in (
        "is_compliant",
        "statement",
        "profile_name",
        "passed_rules",
        "failed_rules",
        "passed_checks",
        "failed_checks",
        "failed_rule_details",
    ):
        summary[key] = parsed.get(key)

    for count_key in ("passed_rules", "failed_rules", "passed_checks", "failed_checks"):
        summary[count_key] = _to_int(summary.get(count_key))

    tagging = []
    other = []
    for rule in summary.get("failed_rule_details") or []:
        if _is_tagging_rule(_rule_blob(rule)):
            tagging.append(rule)
        else:
            other.append(rule)
    summary["tagging_failed_rules"] = tagging
    summary["other_failed_rules"] = other
    summary["raw_report"] = raw

    call_adobe, reason = decide_call_adobe(summary, tagged)
    summary["call_adobe_autotag"] = call_adobe
    summary["decision_reason"] = reason
    return summary


def save_verapdf_artifacts(summary: dict, local_prefix: str) -> tuple[str, str | None]:
    """Write JSON summary and raw report next to the working PDF. Returns paths."""
    os.makedirs(os.path.dirname(local_prefix) or ".", exist_ok=True)
    json_path = f"{local_prefix}_verapdf_summary.json"
    slim = {k: v for k, v in summary.items() if k != "raw_report"}
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(slim, handle, indent=2)
    raw_path = None
    raw = summary.get("raw_report")
    if raw:
        raw_path = f"{local_prefix}_verapdf_report.json"
        with open(raw_path, "w", encoding="utf-8") as handle:
            handle.write(raw)
    return json_path, raw_path
