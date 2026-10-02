import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

function error(status: number, code: string, message: string) {
  return NextResponse.json({ success: false, error: { code, message } }, {
    status, headers: { "Cache-Control": "no-store" },
  });
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  if (path.some(part => !/^[a-zA-Z0-9_-]+$/.test(part))) {
    return error(400, "BAD_REQUEST", "Invalid API path");
  }
  const headers = new Headers();
  for (const name of ["cookie", "origin", "content-type", "x-csrf-token", "if-match"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  try {
    const limit = 101 * 1024 * 1024;
    let body: Uint8Array | undefined;
    if (!["GET", "HEAD"].includes(request.method) && request.body) {
      const reader = request.body.getReader();
      const chunks: Uint8Array[] = []; let size = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > limit) { await reader.cancel(); return error(413, "TOO_LARGE", "Upload limit exceeded"); }
        chunks.push(value);
      }
      body = new Uint8Array(size); let offset = 0;
      for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
    }
    const upstream = process.env.BACKEND_INTERNAL_URL ?? "http://127.0.0.1:8000";
    const response = await fetch(`${upstream}/api/v1/${path.join("/")}${request.nextUrl.search}`, {
      method: request.method, headers, cache: "no-store", redirect: "manual",
      body: body as BodyInit | undefined,
      signal: AbortSignal.timeout(300000),
    });
    const ok = response.ok;
    const json = response.headers.get("content-type")?.includes("application/json");
    // Preserve the existing logout API while keeping browser API responses JSON.
    if (response.status === 204) {
      const result = NextResponse.json({ success: true });
      for (const cookie of response.headers.getSetCookie()) result.headers.append("Set-Cookie", cookie);
      result.headers.set("Cache-Control", "no-store");
      return result;
    }
    if (!json) return error(502, "INVALID_UPSTREAM_RESPONSE", "Backend did not return JSON");
    const data: unknown = await response.json();
    const result = NextResponse.json(data, { status: ok ? response.status : response.status });
    for (const cookie of response.headers.getSetCookie()) result.headers.append("Set-Cookie", cookie);
    for (const name of ["x-request-id", "retry-after", "etag"]) {
      const value = response.headers.get(name);
      if (value) result.headers.set(name, value);
    }
    result.headers.set("Cache-Control", "no-store");
    return result;
  } catch {
    return error(503, "BACKEND_UNAVAILABLE", "تعذر الاتصال بالخادم");
  }
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as DELETE, proxy as PUT };