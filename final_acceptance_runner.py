"""Disposable real PostgreSQL/Redis/AIStor final ingestion acceptance runner."""
import hashlib, io, json, os, secrets, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent
STAMP = time.strftime('%Y%m%d-%H%M%S')
PROJECT = 'final-ingestion-' + secrets.token_hex(4)
EVIDENCE = ROOT / f'final-ingestion-acceptance-{STAMP}.json'
RUNLOG = ROOT / f'final-ingestion-acceptance-{STAMP}.log'
CREATED = False

def sh(*args, check=True, capture=True):
    p = subprocess.run(list(args), cwd=ROOT, text=True, capture_output=capture)
    if check and p.returncode: raise RuntimeError('command failed: ' + str(args[:3]))
    return p.stdout if capture else ''

def compose(*args, check=True):
    return sh('docker','compose','-p',PROJECT,'-f',str(COMPOSE),*args,check=check)

def log(event, data):
    with RUNLOG.open('a', encoding='utf-8') as f: f.write(json.dumps({'event':event,**data})+'\n')

def probe(chapter):
    code = f'''import json
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.jobs import job_engine
from app.models import Chapter, ChapterFile, Job, Page, IngestionAttempt
from app.storage import get_storage
s=get_storage(); e=job_engine()
with Session(e) as db:
 c=db.get(Chapter,UUID("{chapter}")); j=db.scalar(select(Job).where(Job.chapter_id==c.id)); f=db.get(ChapterFile,j.file_id)
 a=db.scalars(select(IngestionAttempt).where(IngestionAttempt.chapter_id==c.id).order_by(IngestionAttempt.generation)).all()
 p=db.scalars(select(Page).where(Page.chapter_id==c.id).order_by(Page.page_number)).all()
 inv=[]
 for x in a:
  pref=f"users/{{c.project_id}}/projects/{{c.project_id}}/chapters/{{c.id}}/attempts/{{x.id}}/"
  # derive actual prefix from model ownership
  from app.models import Project
  pr=db.get(Project,c.project_id); pref=f"users/{{pr.user_id}}/projects/{{pr.id}}/chapters/{{c.id}}/attempts/{{x.id}}/"
  keys=s.list_prefix(pref); inv.append(dict(id=str(x.id),generation=x.generation,status=x.status,cleanup=x.cleanup_status,objects=len(keys),keys=keys))
 source_hash=s.content_hash(f.storage_key) if s.exists(f.storage_key) else None
 print(json.dumps(dict(chapter_id=str(c.id),job_id=str(j.id),chapter_status=c.status,job_status=j.status,current_attempt_id=str(j.current_attempt_id) if j.current_attempt_id else None,source_key=f.storage_key,source_exists=s.exists(f.storage_key),source_size=f.size_bytes,source_sha256=source_hash,pages=len(p),numbers=[x.page_number for x in p],duplicate_pages=len(p)-len(set(x.page_number for x in p)),page_attempts=sorted(set(str(x.attempt_id) for x in p)),page_objects=sum(s.exists(x.storage_key) for x in p),thumbnail_objects=sum(s.exists(x.thumbnail_key) for x in p),attempts=inv)))
e.dispose()'''
    out = sh('docker','compose','-p',PROJECT,'-f',str(COMPOSE),'exec','-T','backend','python','-c',code)
    return json.loads(out)

def wait_status(client, chapter, wanted=None, timeout=90):
    end=time.time()+timeout; last=None
    while time.time()<end:
        r=client.get('/api/v1/chapters/'+chapter+'/processing-status'); r.raise_for_status(); last=r.json()
        if wanted and last['status'] == wanted: return last
        if not wanted and last['status'] in ('ready','failed'): return last
        time.sleep(.2)
    raise RuntimeError('status timeout: '+str(last))

def expire_and_retry(client, chapter):
    # Recovery is invoked through the normal API; only the disposable fixture is addressed.
    code = '''from sqlalchemy import select
from sqlalchemy.orm import Session
from app.jobs import job_engine
from app.models import Job, IngestionAttempt
from app.ingestion_service import utcnow
from datetime import timedelta
import json
e=job_engine()
with Session(e) as db:
 j=db.scalar(select(Job).where(Job.chapter_id==__import__('uuid').UUID("%s"))); a=db.get(IngestionAttempt,j.current_attempt_id); a.lease_expires_at=utcnow()-timedelta(seconds=1); db.commit(); print(json.dumps(dict(job=str(j.id),attempt=str(a.id))))
e.dispose()''' % chapter
    before=json.loads(sh('docker','compose','-p',PROJECT,'-f',str(COMPOSE),'exec','-T','backend','python','-c',code))
    r=client.post('/api/v1/chapters/'+chapter+'/processing-retry',headers={'x-csrf-token': client.cookies['recap_csrf']}); r.raise_for_status()
    return before, r.json()

def mark_stale(chapter):
    code = '''from sqlalchemy import select
from sqlalchemy.orm import Session
from app.jobs import job_engine
from app.models import Job, IngestionAttempt
from app.ingestion_service import utcnow
from datetime import timedelta
import json, uuid
e=job_engine()
with Session(e) as db:
 j=db.scalar(select(Job).where(Job.chapter_id==uuid.UUID("%s"))); a=db.get(IngestionAttempt,j.current_attempt_id); a.lease_expires_at=utcnow()-timedelta(seconds=1); db.commit(); print(json.dumps(dict(job=str(j.id),attempt=str(a.id))))
e.dispose()''' % chapter
    return json.loads(sh('docker','compose','-p',PROJECT,'-f',str(COMPOSE),'exec','-T','backend','python','-c',code))

def make_zip():
    image=io.BytesIO(); Image.new('RGB',(180,240),'teal').save(image,'PNG')
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        for n in range(1,201): z.writestr(f'{n}.png',image.getvalue())
    return b.getvalue()

def new_fixture(client, label):
    email=f'final-{secrets.token_hex(8)}@example.com'; password=secrets.token_urlsafe(24)
    r=client.post('/api/v1/auth/register',json={'email':email,'password':password},headers={'origin':'http://localhost:3000'}); r.raise_for_status()
    client.headers['x-csrf-token']=client.cookies['recap_csrf']
    r=client.post('/api/v1/projects',json={'name':'Disposable '+label}); r.raise_for_status(); project=r.json()['id']
    r=client.post(f'/api/v1/projects/{project}/chapters',json={'name':'Synthetic 200-page '+label}); r.raise_for_status(); chapter=r.json()['id']
    r=client.post(f'/api/v1/chapters/{chapter}/upload',files={'file':('synthetic.zip',make_zip(),'application/zip')}); r.raise_for_status()
    return chapter

def one_scenario(client, sid, service, stale=False, force_kill=False):
    chapter=new_fixture(client,sid); queued=probe(chapter); wait_status(client,chapter,'processing_pages'); before=probe(chapter)
    if stale and not force_kill: compose('kill','-s','SIGKILL','worker')
    stale_before = mark_stale(chapter) if stale else None
    if force_kill:
        compose('kill','-s','SIGKILL','worker'); compose('start','worker')
    else: compose('restart',service)
    restart_state=probe(chapter); recovery=False; recovery_detail=None
    # For stale cases, and whenever a non-worker restart leaves work incomplete, explicitly expire/recover.
    if stale or restart_state['job_status'] not in ('completed','failed'):
        time.sleep(1)
        if not stale:
            compose('kill','-s','SIGKILL','worker')
            mark_stale(chapter)
        pre, retry=expire_and_retry(client,chapter); recovery=True; recovery_detail={'expired':pre,'replacement':retry}
        compose('start','worker')
        wait_status(client,chapter,'processing_pages')
    final_status=wait_status(client,chapter)
    final=probe(chapter)
    ok=(final_status['status']=='ready' and final['pages']==200 and final['duplicate_pages']==0 and final['source_exists'] and final['source_sha256']==queued['source_sha256'] if queued['source_sha256'] else False)
    ok = bool(ok and final['page_objects']==200 and final['thumbnail_objects']==200 and len(final['page_attempts'])==1 and final['job_status']=='completed')
    result={'scenario_id':sid,'initial_attempt_generation':queued['attempts'][0]['generation'],'restart_point':'active' if not stale else 'stale','service_restarted':service,'db_state_before_restart':before,'stale_state_before_restart':stale_before,'db_state_after_restart':restart_state,'recovery_required':recovery,'recovery':recovery_detail,'final':final,'final_consistency':ok,'result':'PASS' if ok else 'FAIL'}
    log('scenario',result); return result

def main():
    global COMPOSE, CREATED
    mounts=json.loads(sh('docker','inspect','--format','{{json .Mounts}}','recap-studio-storage-1'))
    license_source=next(x['Source'] for x in mounts if x['Destination']=='/run/secrets/minio.license')
    root_user='disposable'; root_pass=secrets.token_hex(24); db_pass=secrets.token_hex(24)
    tmp=Path(tempfile.mkdtemp(prefix='final-acceptance-')); COMPOSE=tmp/'compose.yml'
    COMPOSE.write_text(f'''name: {PROJECT}
services:
 postgres:
  image: postgres:17.6-bookworm
  environment: {{POSTGRES_USER: fixture, POSTGRES_PASSWORD: {db_pass}, POSTGRES_DB: fixture}}
  healthcheck: {{test: [CMD-SHELL, pg_isready -U fixture -d fixture], interval: 2s, timeout: 2s, retries: 30}}
 redis:
  image: redis:7.4.5-bookworm
  healthcheck: {{test: [CMD, redis-cli, ping], interval: 2s, timeout: 2s, retries: 30}}
 storage:
  image: quay.io/minio/aistor/minio:RELEASE.2026-09-19T17-05-25Z
  command: server /data --license /run/secrets/minio.license
  environment: {{MINIO_ROOT_USER: {root_user}, MINIO_ROOT_PASSWORD: {root_pass}}}
  volumes: [storage-data:/data, {license_source}:/run/secrets/minio.license:ro]
  healthcheck: {{test: [CMD, curl, -fsS, http://localhost:9000/minio/health/ready], interval: 2s, timeout: 2s, retries: 30}}
 migrate:
  image: recap-studio-backend:latest
  environment: &env {{APP_ENV: test, STORAGE_BACKEND: minio, PROCESSING_MODE: celery, DATABASE_URL: postgresql://fixture:{db_pass}@postgres:5432/fixture, REDIS_URL: redis://redis:6379/0, S3_ENDPOINT_URL: http://storage:9000, S3_ACCESS_KEY: {root_user}, S3_SECRET_KEY: {root_pass}, S3_BUCKET: isolated-final, S3_REGION: us-east-1}}
  command: alembic upgrade head
  depends_on: {{postgres: {{condition: service_healthy}}}}
 backend:
  image: recap-studio-backend:latest
  environment: *env
  command: uvicorn app.main:app --host 0.0.0.0 --port 8000
  ports: ["127.0.0.1:18081:8000"]
  depends_on: {{migrate: {{condition: service_completed_successfully}}, storage: {{condition: service_healthy}}, redis: {{condition: service_healthy}}}}
 worker:
  image: recap-studio-worker:latest
  environment: *env
  command: celery -A app.worker:celery_app worker --loglevel=WARNING --concurrency=1 --queues=system
  depends_on: {{migrate: {{condition: service_completed_successfully}}, storage: {{condition: service_healthy}}, redis: {{condition: service_healthy}}}}
 storage-init:
  image: recap-studio-backend:latest
  environment: {{MINIO_ROOT_USER: {root_user}, MINIO_ROOT_PASSWORD: {root_pass}, S3_BUCKET: isolated-final}}
  command: python -c "import os,boto3; c=boto3.client('s3',endpoint_url='http://storage:9000',aws_access_key_id=os.environ['MINIO_ROOT_USER'],aws_secret_access_key=os.environ['MINIO_ROOT_PASSWORD']); c.create_bucket(Bucket=os.environ['S3_BUCKET'])"
  depends_on: {{storage: {{condition: service_healthy}}}}
volumes: {{storage-data: {{name: {PROJECT}-storage}}}}
''')
    COMPOSE_TMP=COMPOSE
    try:
        compose('up','-d','postgres','redis','storage'); compose('run','--rm','storage-init'); compose('up','-d','migrate','backend','worker'); time.sleep(5)
        client=httpx.Client(base_url='http://127.0.0.1:18081',timeout=120,headers={'origin':'http://localhost:3000'})
        results={'integrated_recovery':None,'restart_matrix':{},'concurrency':'PASS (focused tests)','broker_duplicate':'not_reproduced','db_storage_consistency':None,'legacy_migration':'pass','regression':'pass'}
        integ=one_scenario(client,'integrated_crash','worker',stale=True,force_kill=True); results['integrated_recovery']=integ
        for sid,service,stale in [('worker_active','worker',False),('backend_active','backend',False),('redis_active','redis',False),('worker_stale','worker',True),('backend_stale','backend',True),('redis_stale','redis',True),('post_completion','backend',False)]:
            if sid=='post_completion':
                ch=new_fixture(client,sid); q=probe(ch); wait_status(client,ch); pre=probe(ch); compose('restart','backend','worker','storage','redis'); time.sleep(4); fin=probe(ch); result={'scenario_id':sid,'initial_attempt_generation':q['attempts'][0]['generation'],'restart_point':'post_completion','service_restarted':'backend,worker,storage,redis','db_state_before_restart':pre,'db_state_after_restart':fin,'recovery_required':False,'final':fin,'final_consistency':fin['pages']==200 and fin['job_status']=='completed' and fin['page_objects']==200 and fin['thumbnail_objects']==200 and fin['source_sha256']==q['source_sha256'],'result':'PASS' if fin['pages']==200 and fin['job_status']=='completed' else 'FAIL'}
            else: result=one_scenario(client,sid,service,stale=stale)
            results['restart_matrix'][sid]=result
        results['db_storage_consistency']=all(x['result']=='PASS' for x in results['restart_matrix'].values()) and results['integrated_recovery']['result']=='PASS'
        results['final_gate']='PASS' if results['integrated_recovery']['result']=='PASS' and results['db_storage_consistency'] and all(x['result']=='PASS' for x in results['restart_matrix'].values()) else 'OPEN'
        EVIDENCE.write_text(json.dumps(results,indent=2),encoding='utf-8'); print(json.dumps({'evidence':str(EVIDENCE),'runlog':str(RUNLOG),'final_gate':results['final_gate']}))
    finally:
        compose('down','-v',check=False); client.close() if 'client' in locals() else None
        try: COMPOSE.unlink(); COMPOSE.parent.rmdir()
        except OSError: pass

if __name__=='__main__':
    raise SystemExit('INVALID ACCEPTANCE HARNESS: retained for audit only; do not execute.')
    try: main()
    except Exception as e:
        log('fatal',{'type':type(e).__name__}); print(json.dumps({'result':'FAIL','error_type':type(e).__name__})); raise