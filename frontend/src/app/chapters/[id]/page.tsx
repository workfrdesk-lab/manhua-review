"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";

type Page = { id: string; page_number: number; status: string; thumbnail_url: string; width: number; height: number };
type Chapter = { name: string; status: string };
type Panel = { id: string; panel_index: number; x: number; y: number; width: number; height: number; confidence: number; status: string };
type AnalysisStatus = { status: string; panel_detection_status?: string; ocr_status?: string; visual_analysis_status?: string };
type StoryStatus = { status: string; provider?: string; model?: string; pages_processed?: number; chunks?: number; error?: string };
type Character = { id: string; name: string; display_name: string; description?: string; importance: number; confidence: number };
type Scene = { id: string; scene_index: number; title: string; summary: string; start_page: number; end_page: number; importance: number };
type Event = { id: string; event_index: number; description: string; event_type: string; importance: number };
type Summary = { logline?: string; summary?: string; main_characters?: string[]; major_events?: string[]; conflicts?: string[]; ending_state?: string };
type ScriptEvidence = { page_id?: string; page_number?: number; panel_id?: string; ocr_result_id?: string; scene_ref: string; event_ref: string; quote?: string; reason: string };
type ScriptSegment = { id: string; sequence: number; narration_text: string; estimated_duration: number; dialogue_text?: string; speaker_ref?: string; scene_ref: string; event_refs: string[]; confidence: number; evidence?: ScriptEvidence[] };
type Script = { id: string; story_version_id: string; status: string; title: string; data: { hook?: string; intro?: string; outro?: string }; segments: ScriptSegment[]; evidence: ScriptEvidence[] };
type ScriptJob = { id: string; status: string; script_version_id?: string; error?: string };

function Thumbnail({ page }: { page: Page }) {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    let active = true;
    api<{ data_url: string }>(`pages/${page.id}/thumbnail`).then(result => {
      if (active) setUrl(result.data_url);
    }).catch(() => { if (active) setUrl(undefined); });
    return () => { active = false; };
  }, [page.id]);
  return url ? <img src={url} alt={`Page ${page.page_number}`} className="aspect-[2/3] w-full object-contain" /> : <p>تحميل الصورة…</p>;
}

export default function ChapterPage() {
  const { id } = useParams<{ id: string }>();
  const [chapter, setChapter] = useState<Chapter | null>(null);
  const [pages, setPages] = useState<Page[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [dragged, setDragged] = useState<string | null>(null);
  const [selected, setSelected] = useState<Page | null>(null);
  const [image, setImage] = useState<string>();
  const [panels, setPanels] = useState<Panel[]>([]);
  const [analysis, setAnalysis] = useState<AnalysisStatus | null>(null);
  const [ocr, setOcr] = useState<{ text: string; confidence: number; status: string } | null>(null);
  const [visual, setVisual] = useState<{ data_json?: Record<string, unknown>; status: string } | null>(null);
  const [storyStatus, setStoryStatus] = useState<StoryStatus | null>(null);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [scenes, setScenes] = useState<Scene[]>([]);
  const [events, setEvents] = useState<Record<string, Event[]>>({});
  const [summary, setSummary] = useState<Summary | null>(null);
  const [storyTab, setStoryTab] = useState("Overview");
  const [script, setScript] = useState<Script | null>(null);
  const [scriptJob, setScriptJob] = useState<ScriptJob | null>(null);
  const [scriptError, setScriptError] = useState<string | null>(null);
  const [scriptBusy, setScriptBusy] = useState(false);
  const [scriptVersions, setScriptVersions] = useState<Script[]>([]);
  const [selectedScript, setSelectedScript] = useState("");
  const [segmentDraft, setSegmentDraft] = useState<ScriptSegment | null>(null);
  const [metadataDraft, setMetadataDraft] = useState<{ title: string; hook: string; intro: string; outro: string } | null>(null);
  async function reorder(from: string, to: string) {
    const ordered = pages.filter(page => page.id !== from);
    const moving = pages.find(page => page.id === from);
    if (!moving) return;
    ordered.splice(pages.findIndex(page => page.id === to), 0, moving);
    setBusy(true);
    try {
      await api(`chapters/${id}/pages/reorder`, { method: "PATCH", body: JSON.stringify({ page_ids: ordered.map(page => page.id) }) });
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "فشل الترتيب"); }
    finally { setBusy(false); setDragged(null); }
  }
  async function refresh() {
    try {
      const [value, pageList] = await Promise.all([
        api<Chapter>(`chapters/${id}`), api<Page[]>(`chapters/${id}/pages`),
      ]);
      setChapter(value); setPages(pageList); setError(null);
    } catch (reason) {
      setError(reason instanceof ApiError && reason.status === 401 ? "يرجى تسجيل الدخول" : reason instanceof Error ? reason.message : "فشل التحميل");
    }
  }
  useEffect(() => { refresh(); }, [id]);
  useEffect(() => {
    if (!chapter || ["ready", "failed", "draft"].includes(chapter.status)) return;
    const timer = window.setInterval(refresh, 1500); return () => window.clearInterval(timer);
  }, [chapter?.status, id]);
  async function remove(pageId: string) {
    setBusy(true); try { await api(`pages/${pageId}`, { method: "DELETE" }); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "فشل الحذف"); }
    finally { setBusy(false); }
  }
  async function upload() {
    if (!file) return;
    setBusy(true); setError(null);
    try {
      const form = new FormData(); form.append("file", file);
      await api(`chapters/${id}/upload`, { method: "POST", body: form });
      setFile(null); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "فشل الرفع"); }
    finally { setBusy(false); }
  }
  async function openAnalysis(page: Page) {
    setSelected(page); setError(null); setPanels([]); setOcr(null); setVisual(null);
    try {
      const picture = await api<{ data_url: string }>(`pages/${page.id}/image`);
      setImage(picture.data_url);
      const current = await api<AnalysisStatus>(`pages/${page.id}/analysis-status`);
      setAnalysis(current);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "فشل فتح التحليل"); }
  }
  async function analyzePage() {
    if (!selected) return;
    setBusy(true); setError(null);
    try { setAnalysis(await api<AnalysisStatus>(`pages/${selected.id}/analyze`, { method: "POST", body: JSON.stringify({ reading_direction: "ltr" }) })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "فشل التحليل"); }
    finally { setBusy(false); }
  }
  async function analyzeChapter() {
    setBusy(true); setError(null);
    try {
      for (const page of pages) {
        await api(`pages/${page.id}/analyze`, { method: "POST", body: JSON.stringify({ reading_direction: "ltr" }) });
      }
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "فشل تحليل الفصل"); }
    finally { setBusy(false); }
  }
  async function refreshStory() {
    try {
      const [status, chars, sceneList, chapterSummary] = await Promise.all([
        api<StoryStatus>(`chapters/${id}/story/status`), api<Character[]>(`chapters/${id}/characters`),
        api<Scene[]>(`chapters/${id}/scenes`), api<Summary>(`chapters/${id}/story/summary`),
      ]);
      setStoryStatus(status); setCharacters(chars); setScenes(sceneList); setSummary(chapterSummary);
      const entries = await Promise.all(sceneList.map(async scene => [scene.id, await api<Event[]>(`scenes/${scene.id}/events`)] as const));
      setEvents(Object.fromEntries(entries));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "فشل تحميل فهم القصة"); }
  }
  async function analyzeStory(force = false) {
    setBusy(true); setError(null);
    try { setStoryStatus(await api<StoryStatus>(`chapters/${id}/story/analyze`, { method: "POST", body: JSON.stringify({ force }) })); await refreshStory(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "فشل تحليل القصة"); }
    finally { setBusy(false); }
  }
  useEffect(() => { refreshStory(); }, [id]);
  async function refreshScript() {
    try {
      const [scripts, jobs] = await Promise.all([
        api<Script[]>(`chapters/${id}/scripts`), api<ScriptJob[]>(`chapters/${id}/script-jobs`),
      ]);
      setScriptVersions(scripts);
      setScriptJob(jobs[0] ?? null);
      const chosen = scripts.find(value => value.id === selectedScript) ?? scripts[0];
      if (chosen) setScript(await api<Script>(`scripts/${chosen.id}`));
    } catch (reason) { setScriptError(reason instanceof Error ? reason.message : "فشل تحميل النص"); }
  }
  async function generateScript() {
    setScriptBusy(true); setScriptError(null);
    try {
      const versions = await api<{ id: string }[]>(`chapters/${id}/story/versions`);
      if (!versions[0]) throw new Error("حلّل القصة أولاً");
      const response = await api<{ job: ScriptJob }>(`chapters/${id}/scripts`, { method: "POST", body: JSON.stringify({ story_version_id: versions[0].id }) });
      setScriptJob(response.job);
      setSelectedScript(""); setSegmentDraft(null); setMetadataDraft(null);
      await refreshScript();
    } catch (reason) { setScriptError(reason instanceof Error ? reason.message : "فشل إنشاء النص"); }
    finally { setScriptBusy(false); }
  }
  async function editSegment(segment: ScriptSegment) {
    setScriptBusy(true); setScriptError(null);
    try { await api(`script-segments/${segment.id}`, { method: "PATCH", body: JSON.stringify({ narration_text: segment.narration_text, sequence: segment.sequence, estimated_duration: segment.estimated_duration, confidence: segment.confidence, ...(segment.dialogue_text ? { dialogue_text: segment.dialogue_text } : {}) }) }); setSegmentDraft(null); await refreshScript(); }
    catch (reason) { setScriptError(reason instanceof Error ? reason.message : "فشل حفظ المقطع"); }
    finally { setScriptBusy(false); }
  }
  async function saveMetadata() {
    if (!script || !metadataDraft) return;
    setScriptBusy(true); setScriptError(null);
    try { await api(`scripts/${script.id}/metadata`, { method: "PATCH", body: JSON.stringify(metadataDraft) }); setMetadataDraft(null); await refreshScript(); }
    catch (reason) { setScriptError(reason instanceof Error ? reason.message : "Metadata save failed"); }
    finally { setScriptBusy(false); }
  }
  async function retryScript() {
    if (!scriptJob) return;
    setScriptBusy(true); setScriptError(null);
    try { setScriptJob(await api<ScriptJob>(`script-jobs/${scriptJob.id}/retry`, { method: "POST" })); await refreshScript(); }
    catch (reason) { setScriptError(reason instanceof Error ? reason.message : "Retry failed"); }
    finally { setScriptBusy(false); }
  }
  async function reviewScript(status: "confirmed" | "rejected" | "needs_review") {
    if (!script) return;
    setScriptBusy(true); setScriptError(null);
    try { await api(`scripts/${script.id}/review`, { method: "PATCH", body: JSON.stringify({ status }) }); await refreshScript(); }
    catch (reason) { setScriptError(reason instanceof Error ? reason.message : "فشل تحديث المراجعة"); }
    finally { setScriptBusy(false); }
  }
  useEffect(() => { refreshScript(); }, [id, selectedScript]);
  useEffect(() => {
    if (!scriptJob || !["queued", "running"].includes(scriptJob.status)) return;
    const timer = window.setInterval(refreshScript, 1500); return () => window.clearInterval(timer);
  }, [scriptJob?.status, scriptJob?.id, id, selectedScript]);
  useEffect(() => {
    if (!storyStatus || !["pending", "analyzing"].includes(storyStatus.status)) return;
    const timer = window.setInterval(refreshStory, 1500); return () => window.clearInterval(timer);
  }, [storyStatus?.status, id]);
  useEffect(() => {
    if (!selected || !analysis || ["not_started", "completed", "failed"].includes(analysis.status)) return;
    const timer = window.setInterval(async () => {
      const current = await api<AnalysisStatus>(`pages/${selected.id}/analysis-status`); setAnalysis(current);
      if (current.status === "completed" || current.status === "failed") {
        const found = await api<Panel[]>(`pages/${selected.id}/panels`); setPanels(found);
      }
    }, 1000); return () => window.clearInterval(timer);
  }, [selected?.id, analysis?.status]);
  useEffect(() => {
    if (!selected || analysis?.status !== "completed") return;
    api<Panel[]>(`pages/${selected.id}/panels`).then(setPanels).catch(() => undefined);
  }, [selected?.id, analysis?.status]);
  async function selectPanel(panel: Panel) {
    try {
      const [text, metadata] = await Promise.all([api<{ text: string; confidence: number; status: string }[]>(`panels/${panel.id}/ocr`), api<{ data_json?: Record<string, unknown>; status: string }>(`panels/${panel.id}/analysis`)]);
      setOcr(text[0] ?? null); setVisual(metadata);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "فشل تحميل نتيجة اللوحة"); }
  }
  return <main className="mx-auto max-w-6xl space-y-6 px-6 py-12" dir="rtl">
    <h1 className="text-2xl font-bold">{chapter?.name ?? "الفصل"}</h1>
    <p>الحالة: {chapter?.status ?? "جارٍ التحميل"} — الصفحات: {pages.length}</p>
    <div className="flex flex-wrap items-center gap-3">
      <input type="file" accept=".pdf,.zip,.png,.jpg,.jpeg,application/pdf,application/zip,image/png,image/jpeg"
        onChange={event => setFile(event.target.files?.[0] ?? null)} />
      <button disabled={!file || busy || chapter?.status !== "draft"} onClick={upload}
        className="rounded bg-teal-700 px-4 py-2">رفع ومعالجة</button>
      <button disabled={busy || pages.length === 0} onClick={analyzeChapter}
        className="rounded border border-teal-500 px-4 py-2">Analyze Chapter</button>
      <button disabled={busy || pages.length === 0 || ["pending", "analyzing"].includes(storyStatus?.status ?? "")}
        onClick={() => analyzeStory(false)} className="rounded bg-indigo-700 px-4 py-2">Analyze Story</button>
      <button disabled={busy || ["pending", "analyzing"].includes(storyStatus?.status ?? "")}
        onClick={() => analyzeStory(true)} className="rounded border border-indigo-500 px-4 py-2">Re-analyze Story</button>
    </div>
    {error && <p role="alert" className="text-rose-300">{error}</p>}
    <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
      {pages.map((page, index) => <article key={page.id} draggable={!busy}
        onDragStart={() => setDragged(page.id)} onDragOver={event => event.preventDefault()}
        onDrop={() => { if (dragged && !busy) reorder(dragged, page.id); }}
        className="space-y-2 rounded border border-slate-700 p-2">
        <Thumbnail page={page} />
        <div className="flex justify-between"><span>Page {page.page_number}</span><span>{page.status}</span></div>
        <button disabled={busy} onClick={() => openAnalysis(page)} className="text-teal-300">تحليل الصفحة</button>
        <button disabled={busy} onClick={() => remove(page.id)} className="text-rose-300">حذف</button>
        <button disabled={busy || index === 0} onClick={() => reorder(page.id, pages[index - 1].id)} className="px-3">تحريك للأعلى</button>
      </article>)}
    </div>
    {selected && <section className="grid gap-6 rounded border border-teal-800 p-4 lg:grid-cols-[2fr_1fr]">
      <div><div className="flex items-center justify-between"><h2 className="text-xl font-bold">Page Analysis Viewer</h2><button onClick={analyzePage} disabled={busy || analysis?.status === "processing"} className="rounded bg-teal-700 px-4 py-2">{analysis?.status === "completed" ? "إعادة التحليل" : "Analyze"}</button></div>
        <p className="my-2">Panel Detection: {analysis?.panel_detection_status ?? "queued"} · OCR: {analysis?.ocr_status ?? "queued"} · Visual Analysis: {analysis?.visual_analysis_status ?? "queued"}</p>
        <div className="relative inline-block max-w-full">{image && <img src={image} alt="Page analysis" className="max-h-[800px] max-w-full object-contain" />}{panels.map(panel => <button key={panel.id} onClick={() => selectPanel(panel)} style={{ left: `${panel.x / selected.width * 100}%`, top: `${panel.y / selected.height * 100}%`, width: `${panel.width / selected.width * 100}%`, height: `${panel.height / selected.height * 100}%` }} className="absolute border-2 border-cyan-400 bg-cyan-300/10 text-xs">Panel #{panel.panel_index}</button>)}</div>
      </div><aside><h3 className="font-bold">Panel details</h3>{ocr ? <><p className="mt-3 whitespace-pre-wrap">{ocr.text || "لا يوجد نص"}</p><p>OCR: {ocr.status} ({ocr.confidence.toFixed(2)})</p></> : <p>اختر Panel لرؤية التفاصيل.</p>}{visual?.data_json && <pre className="mt-4 overflow-auto text-xs">{JSON.stringify(visual.data_json, null, 2)}</pre>}</aside>
    </section>}
    <section className="space-y-4 rounded border border-indigo-800 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-xl font-bold">Story Understanding</h2><span>{storyStatus?.status ?? "pending"}{storyStatus?.error ? ` — ${storyStatus.error}` : ""}</span></div>
      <nav className="flex gap-2">{["Overview", "Characters", "Scenes", "Events", "Timeline"].map(tab => <button key={tab} onClick={() => setStoryTab(tab)} className={`rounded px-3 py-1 ${storyTab === tab ? "bg-indigo-700" : "border border-slate-700"}`}>{tab}</button>)}</nav>
      {storyTab === "Overview" && <div className="space-y-2"><h3 className="font-bold">{summary?.logline || "لا يوجد ملخص بعد"}</h3><p>{summary?.summary}</p><p>الشخصيات الرئيسية: {summary?.main_characters?.join("، ") || "—"}</p><p>الأحداث الكبرى: {summary?.major_events?.join(" — ") || "—"}</p><p>الصراعات: {summary?.conflicts?.join(" — ") || "—"}</p><p>حالة النهاية: {summary?.ending_state || "—"}</p></div>}
      {storyTab === "Characters" && <div className="grid gap-3 md:grid-cols-2">{characters.map(character => <article key={character.id} className="rounded border border-slate-700 p-3"><h3 className="font-bold">{character.display_name}</h3><p>{character.description || "لا يوجد وصف"}</p><small>الأهمية {character.importance.toFixed(2)} · الثقة {character.confidence.toFixed(2)}</small></article>)}</div>}
      {(storyTab === "Scenes" || storyTab === "Events" || storyTab === "Timeline") && <div className="space-y-3">{scenes.map(scene => <article key={scene.id} className="rounded border border-slate-700 p-3"><h3 className="font-bold">Scene {scene.scene_index}: {scene.title} <span className="text-sm">(Pages {scene.start_page}–{scene.end_page})</span></h3><p>{scene.summary}</p>{storyTab !== "Scenes" && <ol className="list-decimal pr-6">{(events[scene.id] ?? []).map(event => <li key={event.id}>{event.description} <small>({event.event_type}, {event.importance.toFixed(2)})</small></li>)}</ol>}</article>)}</div>}
    </section>
    <section className="space-y-4 rounded border border-teal-800 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-xl font-bold">Script Review</h2><span>Generation: {scriptJob?.status ?? "not_started"} · Review: {script?.status ?? "—"}</span></div>
      <div className="flex flex-wrap gap-2"><button onClick={generateScript} disabled={scriptBusy || ["queued", "running"].includes(scriptJob?.status ?? "")} className="rounded bg-teal-700 px-3 py-2">Generate script</button>{script && <>{(["confirmed", "rejected", "needs_review"] as const).map(state => <button key={state} disabled={scriptBusy || script.status === state} onClick={() => reviewScript(state)} className="rounded border border-slate-600 px-3 py-2">{state === "confirmed" ? "Confirm" : state === "rejected" ? "Reject" : "Reopen"}</button>)}</>}{scriptJob?.status === "failed" && <button disabled={scriptBusy} onClick={retryScript} className="rounded border border-amber-500 px-3 py-2">Retry</button>}<button disabled={scriptBusy} onClick={refreshScript}>Refresh status</button></div>
      <label className="block">Script version <select className="bg-slate-900 p-2" value={selectedScript || scriptVersions[0]?.id || ""} disabled={scriptBusy} onChange={event => { setSelectedScript(event.target.value); setSegmentDraft(null); setMetadataDraft(null); }}>{scriptVersions.map(value => <option key={value.id} value={value.id}>{value.title} — {value.id} ({value.status})</option>)}</select></label>
      {scriptJob?.error && <p role="alert" className="text-rose-300">{scriptJob.error}</p>}
      {scriptError && <p className="text-rose-300">{scriptError}</p>}
      {script && <>
        <p className="break-all text-sm">ScriptVersion: {script.id} · Source StoryVersion: {script.story_version_id}</p>
        <h3 className="text-lg font-bold">{script.title}</h3>
        {metadataDraft ? <form className="space-y-2" onSubmit={event => { event.preventDefault(); saveMetadata(); }}>
          {(["title", "hook", "intro", "outro"] as const).map(field => <label key={field} className="block">{field}<textarea required={field === "title"} maxLength={field === "title" ? 300 : 5000} className="block w-full rounded bg-slate-900 p-2" value={metadataDraft[field]} onChange={event => setMetadataDraft({ ...metadataDraft, [field]: event.target.value })} /></label>)}
          <p className="text-sm">Hook, intro and outro must be empty or match a grounded segment narration. Edit the segment first to change its wording.</p>
          <button disabled={scriptBusy} className="mr-3 text-teal-300">Save metadata</button><button type="button" disabled={scriptBusy} onClick={() => setMetadataDraft(null)}>Cancel</button>
        </form> : <><p>Hook: {script.data.hook || "—"}</p><p>Intro: {script.data.intro || "—"}</p><p>Outro: {script.data.outro || "—"}</p><button disabled={scriptBusy} onClick={() => setMetadataDraft({ title: script.title, hook: script.data.hook ?? "", intro: script.data.intro ?? "", outro: script.data.outro ?? "" })} className="text-cyan-300">Edit metadata</button></>}
        <ol className="space-y-3 list-decimal pr-6">{script.segments.map(segment => <li key={segment.id} className="rounded border border-slate-700 p-3">
          <div className="flex justify-between"><button disabled={scriptBusy} onClick={() => setSegmentDraft({ ...segment })} className="text-cyan-300">Edit segment</button><span>Confidence {segment.confidence.toFixed(2)} · Duration {segment.estimated_duration}s</span></div>
          {segmentDraft?.id === segment.id ? <form className="space-y-2" onSubmit={event => { event.preventDefault(); editSegment(segmentDraft); }}>
            <label className="block">Narration<textarea required maxLength={5000} className="block w-full bg-slate-900 p-2" value={segmentDraft.narration_text} onChange={event => setSegmentDraft({ ...segmentDraft, narration_text: event.target.value })} /></label>
            {segmentDraft.dialogue_text && <label className="block">Source dialogue<textarea required maxLength={1000} className="block w-full bg-slate-900 p-2" value={segmentDraft.dialogue_text} onChange={event => setSegmentDraft({ ...segmentDraft, dialogue_text: event.target.value })} /></label>}
            {(["sequence", "estimated_duration", "confidence"] as const).map(field => <label key={field} className="block">{field} <input required type="number" step={field === "sequence" ? 1 : 0.01} min={field === "confidence" ? 0 : field === "sequence" ? 1 : 0.01} max={field === "confidence" ? 1 : field === "sequence" ? script.segments.length : 3600} className="bg-slate-900 p-2" value={segmentDraft[field]} onChange={event => setSegmentDraft({ ...segmentDraft, [field]: Number(event.target.value) })} /></label>)}
            <button disabled={scriptBusy} className="mr-3 text-teal-300">Save segment</button><button type="button" disabled={scriptBusy} onClick={() => setSegmentDraft(null)}>Cancel</button>
          </form> : <><p>{segment.narration_text}</p>{segment.dialogue_text && <p>Dialogue: {segment.dialogue_text} {segment.speaker_ref ? `(${segment.speaker_ref})` : ""}</p>}</>}
          <p className="text-sm">Scene: {segment.scene_ref} · Events: {segment.event_refs.join(", ")}</p>
          <div className="mt-2 text-xs text-slate-300">{(segment.evidence ?? []).map((evidence, index) => { const page = pages.find(value => value.id === evidence.page_id || value.page_number === evidence.page_number); return <div key={index}><button disabled={!page} className="text-cyan-300 underline" onClick={() => { if (page) openAnalysis(page); }}>Page {evidence.page_number ?? page?.page_number ?? "—"}</button> · Panel {evidence.panel_id ?? "—"} · OCR {evidence.ocr_result_id ?? "—"} · {evidence.reason}{evidence.quote && <blockquote>{evidence.quote}</blockquote>}</div>; })}</div>
        </li>)}</ol>
      </>}
    </section>
  </main>;
}