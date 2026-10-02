// Same-origin API client. Resolves to the success envelope or throws ApiError.

export class ApiError extends Error {
  constructor(status, code, message, details, requestId) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details ?? {};
    this.requestId = requestId ?? null;
  }
}

export function queryString(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  return search.toString();
}

export async function request(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "Could not reach the API.", {}, null);
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const error = payload?.error ?? {};
    throw new ApiError(
      response.status,
      error.code ?? `HTTP_${response.status}`,
      error.message ?? (response.statusText || "Request failed"),
      error.details,
      payload?.request_id ?? response.headers.get("X-Request-ID"),
    );
  }
  if (!payload || typeof payload !== "object" || !("data" in payload)) {
    throw new ApiError(response.status, "BAD_RESPONSE", "The API returned an unexpected response.", {}, null);
  }
  return payload;
}

export const get = (path, params) => request(`${path}?${queryString(params)}`);
export const post = (path, body) => request(path, { method: "POST", body });
