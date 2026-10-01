import { SystemStatus } from "@/components/system-status";

export default function Home() {
  return (
    <main className="mx-auto min-h-screen max-w-6xl px-5 py-8 sm:px-10 sm:py-12">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 pb-7">
        <div className="flex items-center gap-3">
          <span aria-hidden="true" className="flex h-11 w-11 items-center justify-center rounded-xl bg-teal-400 text-lg font-black text-slate-950">R</span>
          <div><p dir="ltr" className="text-xl font-bold tracking-tight">Recap Studio</p><p className="mt-1 text-xs text-slate-400">من الصفحة إلى الحكاية</p></div>
        </div>
        <span className="rounded-full border border-slate-700 px-4 py-2 text-xs text-slate-300">المرحلة 02 · تأسيس الخدمات</span>
      </header>
      <section className="py-12 sm:py-16">
        <p className="mb-4 text-sm text-teal-300">مساحة العمل / جاهزية النظام</p>
        <h1 className="max-w-3xl text-3xl font-bold leading-relaxed sm:text-5xl sm:leading-snug">أساس موثوق.<br /><span className="text-slate-400">قبل أن تبدأ الحكاية.</span></h1>
        <p className="mt-6 max-w-2xl text-base leading-8 text-slate-400">هذه شاشة تشغيل البنية الأساسية وليست محرر فيديو مكتملًا. تعرض الاتصالات الحقيقية فقط؛ إنشاء المشاريع والرفع والتوليد سيُتاح بعد تنفيذ المراحل التالية واختبارها.</p>
      </section>
      <SystemStatus />
      <section className="mt-6 grid gap-5 sm:grid-cols-2">
        <article className="rounded-2xl border border-slate-800 p-6">
          <h2 className="font-semibold">لا نتائج مصطنعة</h2>
          <p className="mt-3 text-sm leading-7 text-slate-400">لن يظهر فيديو أو رصيد أو مشروع تجريبي باعتباره نتيجة حقيقية. الميزات غير المنفذة لا تُعرض كأزرار قابلة للاستخدام.</p>
        </article>
        <article className="rounded-2xl border border-amber-900/50 bg-amber-950/10 p-6">
          <h2 className="font-semibold text-amber-200">حقوق المحتوى أولًا</h2>
          <p className="mt-3 text-sm leading-7 text-slate-400">أنت مسؤول عن امتلاك حقوق المحتوى أو الإذن اللازم لاستخدامه. لا يوفّر التطبيق تنزيلًا تلقائيًا لفصول المانهوا من مصادر غير مصرّح بها.</p>
        </article>
      </section>
      <footer className="mt-12 text-xs leading-6 text-slate-500">جاهزية الخدمات لا تعني اكتمال وظائف إنشاء الفيديو. هذه النسخة مخصصة للتطوير المحلي.</footer>
    </main>
  );
}