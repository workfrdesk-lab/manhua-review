"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type Health = {
  status: "ready" | "degraded";
  services: { name: string; status: "up" | "down"; latency_ms: number }[];
};

const labels: Record<string, { title: string; description: string }> = {
  postgres: { title: "PostgreSQL", description: "اتصال قاعدة البيانات" },
  redis: { title: "Redis", description: "اتصال وسيط الوظائف" },
  storage: { title: "Object Storage", description: "الوصول إلى حاوية الملفات الخاصة" },
  worker: { title: "Celery Worker", description: "استجابة عامل المعالجة الخلفية" },
};

function isHealth(value: unknown): value is Health {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Health;
  return ["ready", "degraded"].includes(candidate.status) &&
    Array.isArray(candidate.services) && candidate.services.length === 4 &&
    new Set(candidate.services.map((service) => service?.name)).size === 4 &&
    candidate.services.every((service) => service && Object.hasOwn(labels, service.name) &&
      ["up", "down"].includes(service.status) && Number.isFinite(service.latency_ms));
}

export function SystemStatus() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);
  const activeRequest = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setLoading(true);
    try {
      const response = await fetch("/api/v1/system/status", {
        cache: "no-store", signal: controller.signal,
      });
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) {
        throw new Error("unavailable");
      }
      const result: unknown = await response.json();
      if (!response.ok || !isHealth(result)) throw new Error("unavailable");
      setHealth(result);
      setError(null);
      setCheckedAt(new Date().toLocaleTimeString("ar"));
    } catch {
      if (controller.signal.aborted) return;
      setHealth(null);
      setError("تعذر قراءة حالة النظام. تأكد من تشغيل backend ثم أعد المحاولة.");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = setInterval(() => { void refresh(); }, 15000);
    return () => { clearInterval(interval); activeRequest.current?.abort(); };
  }, [refresh]);

  return (
    <section aria-labelledby="services-heading" className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5 sm:p-8">
      <div className="mb-7 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 id="services-heading" className="text-lg font-bold">حالة الخدمات</h2>
          <p className="mt-2 text-sm text-slate-400">فحوص اتصال فعلية من الخادم، تُحدّث كل 15 ثانية.</p>
        </div>
        <button onClick={() => void refresh()} disabled={loading}
          className="rounded-xl border border-slate-700 px-5 py-2 text-sm transition hover:border-teal-400 disabled:opacity-50">
          {loading ? "جارٍ الفحص…" : "تحديث الحالة"}
        </button>
      </div>
      <div aria-live="polite" aria-busy={loading}>
        {error && <p role="alert" className="rounded-xl border border-rose-800 bg-rose-950/30 p-5 text-rose-200">{error}</p>}
        {!health && !error && <p className="py-8 text-slate-400">بانتظار نتائج الاتصال…</p>}
        {health && <>
          <p className={`mb-5 text-sm ${health.status === "ready" ? "text-teal-300" : "text-amber-300"}`}>
            {health.status === "ready" ? "الخدمات الأساسية متصلة" : "توجد خدمات غير جاهزة — راجع إعدادات التشغيل والسجلات"}
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            {health.services.map((service) => (
              <div key={service.name} className="rounded-xl border border-slate-800 bg-[#0b1220] p-5">
                <div className="flex items-center justify-between gap-3">
                  <h3 dir="ltr" className="font-semibold">{labels[service.name].title}</h3>
                  <span className={`rounded-full px-3 py-1 text-xs ${service.status === "up" ? "bg-teal-950 text-teal-300" : "bg-rose-950 text-rose-300"}`}>
                    {service.status === "up" ? "متصل" : "غير متاح"}
                  </span>
                </div>
                <p className="mt-4 text-sm text-slate-400">{labels[service.name].description}</p>
                <p className="mt-2 text-xs text-slate-500">زمن الفحص: {service.latency_ms} ms</p>
              </div>
            ))}
          </div>
          <p className="mt-5 text-xs text-slate-500">آخر قراءة: {checkedAt} · قد تُخزّن نتائج الفحص لمدة 10 ثوانٍ.</p>
        </>}
      </div>
    </section>
  );
}