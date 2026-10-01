export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const csrf = document.cookie.split("; ").find(value => value.startsWith("recap_csrf="));
  if (csrf) headers.set("X-CSRF-Token", decodeURIComponent(csrf.slice("recap_csrf=".length)));
  const response = await fetch(`/api/v1/${path}`, {
    ...init, headers, credentials: "same-origin", cache: "no-store",
  });
  const ok = response.ok;
  const isJson = response.headers.get("content-type")?.includes("application/json");
  if (!isJson) throw new ApiError(response.status, "استجابة غير صالحة من الخادم");
  const value = await response.json();
  if (!ok) throw new ApiError(response.status, value?.error?.message ?? "فشل الطلب");
  return value as T;
}