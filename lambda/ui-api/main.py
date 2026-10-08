"""
UI API: dummy login + presigned S3 access on the existing PDF bucket.

Upload lands in uploads/ (does not start remediating).
Start copies uploads/{file} -> pdf/{file}, which triggers the existing splitter.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
secrets = boto3.client("secretsmanager")

BUCKET = os.environ["BUCKET_NAME"]
DEMO_USER = os.environ.get("DEMO_USER", "amol.ganjare@sparient.com")
DEMO_PASS = os.environ.get("DEMO_PASS", "sparient123")
PRESIGN_SECONDS = int(os.environ.get("PRESIGN_SECONDS", "300"))
TOKEN_TTL = int(os.environ.get("TOKEN_TTL_SECONDS", str(12 * 3600)))

_SECRET_CACHE = None
# CORS is set on the Function URL only. Do not also send Access-Control-Allow-Origin
# here or the browser sees two values (*, CloudFront origin) and blocks login.


def lambda_handler(event, context):
    method = (
        (event.get("requestContext") or {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()
    path = (event.get("rawPath") or event.get("path") or "/").rstrip("/") or "/"
    qs = event.get("queryStringParameters") or {}
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}

    if method == "OPTIONS":
        return respond(204, "")

    try:
        if method == "POST" and path == "/login":
            return login(parse_body(event))
        if method == "POST" and path == "/upload-url":
            require_auth(headers)
            return upload_url(parse_body(event))
        if method == "POST" and path == "/start":
            require_auth(headers)
            return start_remediation(parse_body(event))
        if method == "GET" and path == "/list":
            require_auth(headers)
            return list_files()
        if method == "GET" and path == "/status":
            require_auth(headers)
            return file_status(qs.get("file") or qs.get("filename") or "")
        if method == "GET" and path == "/download-url":
            require_auth(headers)
            return download_url(qs.get("key") or "")
        return respond(404, {"error": "Unknown route"})
    except AuthError as exc:
        return respond(exc.status, {"error": str(exc)})
    except ValueError as exc:
        return respond(400, {"error": str(exc)})
    except ClientError as exc:
        return respond(500, {"error": exc.response["Error"].get("Message", str(exc))})
    except Exception as exc:
        return respond(500, {"error": str(exc)})


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def parse_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw:
        return {}
    return json.loads(raw)


def respond(status, body):
    payload = "" if body == "" else json.dumps(body)
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": payload,
    }


def token_secret():
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE
    arn = os.environ.get("TOKEN_SECRET_ARN")
    if arn:
        _SECRET_CACHE = secrets.get_secret_value(SecretId=arn)["SecretString"]
    else:
        _SECRET_CACHE = os.environ.get("TOKEN_SECRET", "")
    if not _SECRET_CACHE:
        raise AuthError("Server token secret is not configured", 500)
    return _SECRET_CACHE


def make_token():
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{DEMO_USER}.{exp}"
    sig = hmac.new(token_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def require_auth(headers):
    auth = headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise AuthError("Sign in required")
    token = auth.split(" ", 1)[1].strip()
    try:
        user, exp, sig = token.split(".")
        payload = f"{user}.{exp}"
        expect = hmac.new(token_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig) or int(exp) < time.time():
            raise AuthError("Session expired")
    except AuthError:
        raise
    except Exception:
        raise AuthError("Invalid session")


def is_allowed_login(user, password):
    if password != DEMO_PASS:
        return False
    email = user.strip().lower()
    if email == DEMO_USER.lower():
        return True
    return email.endswith("@sparient.com") and email.count("@") == 1 and ".." not in email


def login(body):
    user = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not is_allowed_login(user, password):
        raise AuthError("Use your @sparient.com email and password")
    return respond(200, {"token": make_token(), "expires_in": TOKEN_TTL})


def safe_pdf_name(name):
    name = os.path.basename(str(name or "")).strip()
    if not name.lower().endswith(".pdf"):
        raise ValueError("Only PDF files are allowed")
    if not name or ".." in name or "/" in name or "\\" in name:
        raise ValueError("Invalid file name")
    if len(name) > 180:
        raise ValueError("File name is too long")
    return name


def stem(name):
    return name[:-4] if name.lower().endswith(".pdf") else name


def object_exists(key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def list_prefix(prefix):
    keys = []
    token = None
    while True:
        kwargs = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for item in resp.get("Contents") or []:
            keys.append(item["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


def allowed_download_key(key):
    key = urllib.parse.unquote(key or "").lstrip("/")
    if ".." in key or key.startswith("/") or "//" in key:
        return None
    if key.startswith("uploads/") and key.lower().endswith(".pdf"):
        return key
    if key.startswith("pdf/") and key.lower().endswith(".pdf"):
        return key
    if key.startswith("result/") and key.lower().endswith(".pdf"):
        return key
    if "/accessability-report/" in key and key.startswith("temp/") and key.lower().endswith(".json"):
        return key
    return None


def upload_url(body):
    name = safe_pdf_name(body.get("filename"))
    key = f"uploads/{name}"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key, "ContentType": "application/pdf"},
        ExpiresIn=PRESIGN_SECONDS,
        HttpMethod="PUT",
    )
    return respond(200, {"url": url, "key": key, "file": name})


def start_remediation(body):
    name = safe_pdf_name(body.get("filename") or body.get("file"))
    source = f"uploads/{name}"
    dest = f"pdf/{name}"
    if not object_exists(source):
        raise ValueError("Upload the PDF before starting remediating")
    status = describe_file(name)
    if status["status"] == "running":
        return respond(200, {**status, "message": "Already running"})
    s3.copy_object(
        Bucket=BUCKET,
        CopySource={"Bucket": BUCKET, "Key": source},
        Key=dest,
        ContentType="application/pdf",
        MetadataDirective="REPLACE",
    )
    return respond(200, describe_file(name))


def download_url(key):
    allowed = allowed_download_key(key)
    if not allowed:
        raise AuthError("That file is not available to download", 403)
    if not object_exists(allowed):
        raise ValueError("File is not ready yet")
    filename = os.path.basename(allowed)
    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET,
            "Key": allowed,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=PRESIGN_SECONDS,
    )
    return respond(200, {"url": url, "key": allowed, "filename": filename})


def discovered_pdf_names():
    names = set()
    for key in list_prefix("uploads/"):
        if key.lower().endswith(".pdf") and key.count("/") == 1:
            names.add(os.path.basename(key))
    for key in list_prefix("pdf/"):
        if key.lower().endswith(".pdf") and key.count("/") == 1:
            names.add(os.path.basename(key))
    for key in list_prefix("result/"):
        base = os.path.basename(key)
        if base.lower().endswith(".pdf") and base.upper().startswith("COMPLIANT_"):
            names.add(base[len("COMPLIANT_"):])
    seen_folders = set()
    for key in list_prefix("temp/"):
        parts = key.split("/")
        if len(parts) < 2 or parts[0] != "temp" or not parts[1]:
            continue
        folder = parts[1]
        if folder in seen_folders:
            continue
        seen_folders.add(folder)
        if folder.lower().endswith(".pdf"):
            names.add(folder)
        else:
            names.add(f"{folder}.pdf")
    return sorted(names)


def list_files():
    files = []
    for name in discovered_pdf_names():
        try:
            files.append(describe_file(name))
        except ValueError:
            continue
    return respond(200, {"files": files})


def file_status(name):
    return respond(200, describe_file(safe_pdf_name(name)))


def report_keys(name):
    prefix = f"temp/{stem(name)}/accessability-report/"
    found = {
        "afterReport": None,
        "remediationStats": None,
        "verapdfReport": None,
        "verapdfSummary": None,
    }
    for key in list_prefix(prefix):
        lower = key.lower()
        if lower.endswith("_accessibility_report_after_remidiation.json"):
            found["afterReport"] = key
        elif lower.endswith("_remediation_stats.json"):
            found["remediationStats"] = key
        elif lower.endswith("_verapdf_report.json"):
            found["verapdfReport"] = key
        elif lower.endswith("_verapdf_summary.json"):
            found["verapdfSummary"] = key
    return found


def categories_from_s3(key):
    if not key:
        return []
    try:
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        doc = json.loads(body)
    except Exception:
        return []
    detailed = doc.get("Detailed Report") or doc.get("detailedReport") or {}
    rows = []
    if not isinstance(detailed, dict):
        return rows
    for cat_name, rules in detailed.items():
        passed = failed = manual = 0
        for rule in rules or []:
            status = str((rule or {}).get("Status") or "").lower()
            if status == "passed":
                passed += 1
            elif status == "failed":
                failed += 1
            elif "manual" in status:
                manual += 1
        rows.append({
            "name": cat_name,
            "passed": passed,
            "failed": failed,
            "needs_manual_check": manual,
        })
    return rows


def describe_file(name):
    name = safe_pdf_name(name)
    uploads_key = f"uploads/{name}"
    pdf_key = f"pdf/{name}"
    original_key = uploads_key if object_exists(uploads_key) else (pdf_key if object_exists(pdf_key) else None)
    result_key = f"result/COMPLIANT_{name}"
    reports = report_keys(name)
    temp_keys = list_prefix(f"temp/{stem(name)}/")
    has_result = object_exists(result_key)
    has_pdf = object_exists(pdf_key)
    has_temp = bool(temp_keys)

    if has_result:
        status, step, percent = "done", "Done", 100
    elif reports.get("remediationStats"):
        status, step, percent = "running", "Post-check", 90
    elif reports.get("verapdfSummary") or reports.get("verapdfReport"):
        status, step, percent = "running", "Remediating", 45
    elif has_temp:
        status, step, percent = "running", "Splitting", 25
    elif has_pdf:
        status, step, percent = "running", "Starting", 10
    elif original_key:
        status, step, percent = "ready", "Ready", 0
    else:
        status, step, percent = "missing", "Not found", 0

    categories = []
    if reports.get("afterReport"):
        categories = categories_from_s3(reports["afterReport"])

    return {
        "file": name,
        "status": status,
        "step": step,
        "percent": percent,
        "original_key": original_key,
        "result_key": result_key if has_result else None,
        "reports": reports,
        "categories": categories,
    }
