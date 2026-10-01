# Recap Studio

تطبيق قيد البناء لإنشاء فيديوهات سرد المانهوا من محتوى يملك المستخدم حق استخدامه.

## الحالة الفعلية

- المرحلة 1: مراجعة ManhwaForge وقرار المعمارية موثقان.
- المرحلة 2: هيكل Next.js وFastAPI وCelery وCompose، وشاشة حالة خدمات متصلة بالـAPI.
- المرحلة 3 بدأت: SQLAlchemy async وAlembic وتسجيل البريد وكلمة المرور وجلسات cookies. لا يوجد رفع أو توليد فيديو بعد، ولا واجهة تسجيل دخول حتى الآن.
- **ليس جاهزًا للإنتاج.** نجاح health checks لا يعني اكتمال المنتج.
- Docker غير متاح في بيئة التنفيذ الحالية؛ تشغيل Compose وتكامل الخدمات لم يُختبرا.

## التشغيل المحلي باستخدام Docker

يلزم Docker Engine أو Docker Desktop مع Linux containers وCompose v2.
من مجلد المشروع:

```powershell
Set-Location 'D:\projects\manhua review'
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 backend worker storage-init
```

الإعدادات الافتراضية للتطوير المحلي فقط، والمنافذ مربوطة بـ127.0.0.1.
يمكن تشغيل `docker compose up -d` مباشرة؛ يبني Compose الصور غير الموجودة.

- الواجهة: http://localhost:3000
- API docs: http://localhost:8000/docs
- Liveness: http://localhost:8000/health/live
- Readiness: http://localhost:8000/health/ready
- MinIO console: http://localhost:9001

`storage-init` ينشئ bucket خاصًا ويمنع الوصول العام. يحتفظ Compose ببيانات PostgreSQL وRedis وMinIO في volumes. لا تستخدم `docker compose down -v` إلا إذا أردت حذف بيانات التطوير.

## التشغيل دون Docker

هذا يشغّل الويب فقط؛ الجاهزية تتطلب PostgreSQL وRedis وS3 وCelery حقيقية.
Celery مخصص هنا لحاويات Linux، وليس لتشغيل production على Windows.

```powershell
Set-Location 'D:\projects\manhua review'
Copy-Item '.env.example' '.env'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

في نافذة أخرى:

```powershell
Set-Location 'D:\projects\manhua review\frontend'
npm ci
npm run dev
```

الواجهة تعرض الأخطاء الفعلية عند غياب الخدمات، ولا تستبدلها ببيانات وهمية.

## متغيرات البيئة

راجع `D:\projects\manhua review\.env.example`.

| المتغير | الغرض |
|---|---|
| APP_ENV | اسم البيئة؛ لا يفعّل حماية production تلقائيًا |
| POSTGRES_USER / PASSWORD / DB | إنشاء قاعدة التطوير في Compose |
| DATABASE_URL | اتصال backend الأصلي؛ Compose يضبط عنوان الخدمة الداخلي |
| REDIS_URL | وسيط Celery وفحص Redis؛ Compose يضبطه داخليًا |
| S3_ENDPOINT_URL | عنوان S3؛ Compose يستخدم MinIO |
| S3_ACCESS_KEY / S3_SECRET_KEY | بيانات اتصال التخزين للتشغيل الأصلي |
| S3_BUCKET / S3_REGION | اسم الحاوية والمنطقة |
| MINIO_ROOT_USER / MINIO_ROOT_PASSWORD | بيانات MinIO المحلية؛ يستخدمها backend في Compose فقط |
| BACKEND_INTERNAL_URL | عنوان backend من خادم Next.js، ليس متغيرًا مكشوفًا للمتصفح |
| LOG_LEVEL | DEBUG / INFO / WARNING / ERROR |

ملف `.env` الجذري يُقرأ بواسطة backend عند تشغيله من الجذر، وبواسطة Compose للاستبدال. Next.js يحتاج المتغير في بيئة العملية أو `frontend/.env.local` عند تغيير عنوانه الافتراضي. لا تحفظ مفاتيح حقيقية في Git. لا تنشر بيانات التطوير الافتراضية.

## الاختبارات

```powershell
Set-Location 'D:\projects\manhua review'
.\.venv\Scripts\python.exe -m pytest -q backend\tests
.\.venv\Scripts\python.exe -m ruff check backend
Set-Location 'D:\projects\manhua review\frontend'
npm run typecheck
npm run build
```

الاختبارات المعزولة تستبدل فحوص الشبكة عمدًا للتحقق من حالات النجاح والفشل والتخزين المؤقت وعدم تسريب الخطأ. ليست بديلًا لاختبار خدمات Compose الحقيقية. يوجد تحذير upstream من Starlette بشأن استخدام httpx في TestClient.

## حدود الأمان الحالية

لا تعرض هذا الإصدار للإنترنت: تحديد المعدل والصلاحيات التفصيلية لم يُنفذا بعد. توجد مصادقة أساسية وCSRF مرتبط بالجلسة وفحص Origin. status endpoint عام محليًا ويكشف أسماء الخدمات وحالتها فقط، لا بيانات اعتمادها. الفحوص مخزنة مؤقتًا 10 ثوانٍ. يجب إضافة صلاحيات قبل تحويله إلى لوحة إدارة.

ملف Python lock يثبت إصدارات بيئة الاختبار الحالية، دون hashes، ولم يُثبت بعد داخل Linux/Python 3.12؛ هذا جزء من بوابة قبول Compose. MinIO خدمة خارجية لها ترخيصها الخاص؛ لا تُعتبر رخصتها رخصة التطبيق. يلزم تدقيق تراخيص وصور واعتماديات قبل الإنتاج.

## المرحلة التالية

تشغيل Compose والتحقق من PostgreSQL وRedis وMinIO والـworker ما زال مطلوبًا. لاستكمال المرحلة 3: تحديد المعدل، إعدادات المستخدم والصلاحيات وownership، واجهة المصادقة، والتحقق من البريد واستعادة كلمة المرور وOAuth. لا تُعد المرحلة 2 مكتملة القبول قبل التكامل الفعلي.

## المصادقة وقاعدة البيانات — تنفيذ جزئي للمرحلة 3

الجداول الحالية: `users` و`sessions` و`oauth_identities`. جدول OAuth تمهيدي فقط؛ لا توجد تدفقات OAuth بعد. لا تُنشأ الجداول تلقائيًا عند بدء API.

بعد إعداد متغيرات البيئة من الجذر، نفّذ migration قبل استخدام المصادقة:

```powershell
& 'D:\projects\manhua review\.venv\Scripts\python.exe' -m alembic -c 'D:\projects\manhua review\backend\alembic.ini' upgrade head
```

في Docker بعد بناء الصور: `docker compose run --rm backend alembic upgrade head`.

المسارات: `POST /api/v1/auth/register` و`POST /api/v1/auth/login` و`GET /api/v1/auth/me` و`POST /api/v1/auth/logout`.
التسجيل يتطلب `email` و`password` (12–128 حرفًا)، و`display_name` اختياري. كلمات المرور Argon2id، ورمز الجلسة عشوائي ولا يُخزن إلا SHA-256 له.

كل POST للمصادقة يتطلب `Origin` مطابقًا لقائمة `ALLOWED_ORIGINS`، بما في ذلك عملاء سطر الأوامر. تسجيل الخروج يتطلب أيضًا `X-CSRF-Token` بقيمة cookie `recap_csrf`؛ يُتحقق من ارتباطها بالجلسة. cookie الجلسة `HttpOnly` و`SameSite=Lax`، وتُفرض `Secure` في production. مدة الجلسة الافتراضية 30 يومًا. يجب ضبط origins وHTTPS للنشر؛ لا يوجد CORS أو ربط واجهة مصادقة بعد.

التحقق المحلي الحالي: 11 اختبارًا ناجحًا، تشمل دورة الجلسة والإبطال وكلمة المرور الخاطئة وتكرار البريد ورفض Origin وCSRF المزوّر وتعطيل المستخدم وانتهاء الجلسة، وAlembic upgrade/check/downgrade/upgrade على SQLite. هذه ليست اختبارات PostgreSQL أو Compose؛ Docker غير متاح محليًا. Ruff ناجح.

تفاصيل المرجع والقرارات: `D:\projects\manhua review\docs\architecture.md`.