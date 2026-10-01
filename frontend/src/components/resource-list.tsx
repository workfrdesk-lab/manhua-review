"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export function ResourceList({ projectId }: { projectId?: string }) {
  const path = projectId ? `projects/${projectId}/chapters` : "projects";
  const [items, setItems] = useState<{ id: string; name: string; status: string }[]>([]);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function refresh() { setItems(await api<typeof items>(path)); }
  useEffect(() => { refresh().catch(reason => setError(String(reason))); }, [path]);
  async function create(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try { await api(path, { method: "POST", body: JSON.stringify({ name }) }); setName(""); await refresh(); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  return <section className="space-y-4">
    <h2 className="text-xl">{projectId ? "الفصول" : "المشاريع"}</h2>
    {error && <p role="alert">{error}</p>}
    <form onSubmit={create} className="flex gap-3">
      <input aria-label="الاسم" required maxLength={200} value={name} onChange={event => setName(event.target.value)} className="rounded border p-2" />
      <button disabled={busy} className="rounded bg-teal-700 px-4">إنشاء</button>
    </form>
    <ul>{items.map(item => <li key={item.id} className="py-2"><Link href={`/${projectId ? "chapters" : "projects"}/${item.id}`}>{item.name} — {item.status}</Link></li>)}</ul>
  </section>;
}