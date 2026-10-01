import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET() {
  const origin = process.env.BACKEND_INTERNAL_URL ?? "http://127.0.0.1:8000";
  try {
    const response = await fetch(`${origin}/api/v1/system/status`, {
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) {
      throw new Error("Backend unavailable");
    }
    return NextResponse.json(await response.json(), {
      headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json(
      { error: "تعذر الاتصال بالخادم. تحقق من تشغيل خدمة backend ثم أعد المحاولة." },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}