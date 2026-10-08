const TOKEN_KEY = "pdf-remediation-api-token";

export function apiBase() {
  return String(window.PDF_API_BASE || "").replace(/\/$/, "");
}

export function isLive() {
  return Boolean(apiBase());
}

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

async function request(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${apiBase()}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { error: text || "Request failed" };
  }
  if (!response.ok) {
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return data;
}

export async function apiLogin(username, password) {
  const data = await request("POST", "/login", { username, password });
  sessionStorage.setItem(TOKEN_KEY, data.token);
  return data;
}

export function apiUploadUrl(filename) {
  return request("POST", "/upload-url", { filename });
}

export function apiStart(filename) {
  return request("POST", "/start", { filename });
}

export function apiList() {
  return request("GET", "/list");
}

export function apiStatus(filename) {
  return request("GET", `/status?file=${encodeURIComponent(filename)}`);
}

export function apiDownloadUrl(key) {
  return request("GET", `/download-url?key=${encodeURIComponent(key)}`);
}

export async function putPdf(url, file) {
  const response = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/pdf" },
    body: file,
  });
  if (!response.ok) {
    throw new Error("Upload to S3 failed");
  }
}

export async function downloadS3(key) {
  const { url, filename } = await apiDownloadUrl(key);
  const response = await fetch(url);
  if (!response.ok) throw new Error("Download failed");
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename || key.split("/").pop();
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
}
