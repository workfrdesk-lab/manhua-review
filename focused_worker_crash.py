"""Disposable real Celery worker-crash characterization; no application changes."""
import io
import json
import secrets
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).parent


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError('docker command failed')
    return result.stdout


def remote(code):
    result = subprocess.run(
        ['docker', 'exec', '-i', 'recap-studio-backend-1', 'python', '-'],
        input=code, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError('remote assertion failed')
    return json.loads(result.stdout)


def request(client, method, path, **kwargs):
    response = client.request(method, '/api/v1' + path, **kwargs)
    if not response.is_success:
        raise RuntimeError(f'{method} {path} returned {response.status_code}')
    return response.json()


def make_zip():
    image = io.BytesIO()
    Image.new('RGB', (1800, 2400), 'teal').save(image, 'PNG')
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as stream:
        for number in range(1, 201):
            stream.writestr(f'{number}.png', image.getvalue())
    return archive.getvalue()


def status(client, chapter):
    return request(client, 'GET', f'/chapters/{chapter}/processing-status')


def wait_for_processing(client, chapter):
    for _ in range(240):
        value = status(client, chapter)
        if value['status'] == 'processing_pages':
            return value
        if value['status'] in {'ready', 'failed'}:
            raise RuntimeError(f'job did not reach processing_pages: {value}')
        time.sleep(0.25)
    raise RuntimeError('processing_pages was not observed')


def inspect(chapter, user):
    return remote(f'''
import json
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.jobs import job_engine
from app.models import Chapter, ChapterFile, Job, Page, Project
from app.storage import get_storage
s=get_storage()
with Session(job_engine()) as db:
 ch=db.get(Chapter,UUID('{chapter}'))
 job=db.scalar(select(Job).where(Job.chapter_id==UUID('{chapter}')))
 source=db.scalar(select(ChapterFile).where(ChapterFile.chapter_id==UUID('{chapter}')))
 pages=db.scalars(select(Page).where(Page.chapter_id==UUID('{chapter}')).order_by(Page.page_number)).all()
 prefix='users/{user}/projects/'+str(ch.project_id)+'/chapters/{chapter}/'
 keys=[]
 for batch in s.client.get_paginator('list_objects_v2').paginate(Bucket=s.bucket,Prefix=prefix):
  keys += [item['Key'] for item in batch.get('Contents',[])]
 referenced=[source.storage_key] + [key for page in pages for key in (page.storage_key,page.thumbnail_key)]
 print(json.dumps({{
  'job_status': job.status, 'job_error': bool(job.error), 'chapter_status': ch.status,
  'page_rows': len(pages), 'page_numbers': [p.page_number for p in pages],
  'original_exists': s.exists(source.storage_key), 'original_key_present': source.storage_key in keys,
  's3_object_count': len(keys), 'referenced_object_count': len(referenced),
  'unreferenced_object_count': len(set(keys)-set(referenced)),
  'page_objects_present': all(s.exists(p.storage_key) and s.exists(p.thumbnail_key) for p in pages),
  'all_keys_scoped': all(k.startswith(prefix) for k in keys),
 }}))
''')


client = httpx.Client(base_url='http://localhost:8001', headers={'origin': 'http://localhost:3000'}, timeout=120)
user = None
try:
    account = request(client, 'POST', '/auth/register', json={
        'email': f'crash-{uuid.uuid4().hex}@example.com',
        'password': secrets.token_urlsafe(24),
    })
    user = account['id']
    client.headers['x-csrf-token'] = client.cookies['recap_csrf']
    project = request(client, 'POST', '/projects', json={'name': 'disposable crash characterization'})
    chapter = request(client, 'POST', f"/projects/{project['id']}/chapters", json={'name': '200-page crash fixture'})
    upload = request(client, 'POST', f"/chapters/{chapter['id']}/upload", files={
        'file': ('crash.zip', make_zip(), 'application/zip'),
    })
    print('submitted:', json.dumps({'user_created': True, 'job_id_present': bool(upload['job_id'])}), flush=True)
    before = wait_for_processing(client, chapter['id'])
    print('before_worker_termination:', json.dumps(before), flush=True)
    docker('kill', 'recap-studio-worker-1')
    docker('start', 'recap-studio-worker-1')
    print('worker_restarted:', True, flush=True)
    time.sleep(30)
    after = status(client, chapter['id'])
    print('after_bounded_wait:', json.dumps(after), flush=True)
    print('database_and_storage_after_crash:', json.dumps(inspect(chapter['id'], user)), flush=True)
    redispatch = remote(f'''
import json
from app.jobs import get_job_queue
get_job_queue().submit('{upload['job_id']}')
print(json.dumps({{'submitted': True}}))
''')
    print('explicit_redispatch:', json.dumps(redispatch), flush=True)
    time.sleep(15)
    print('after_explicit_redispatch_wait:', json.dumps(status(client, chapter['id'])), flush=True)
    print('database_and_storage_after_redispatch:', json.dumps(inspect(chapter['id'], user)), flush=True)
finally:
    if user:
        print(remote(f'''
import json
from uuid import UUID
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from app.jobs import job_engine
from app.models import User
from app.storage import get_storage
s=get_storage(); engine=job_engine(); uid=UUID('{user}')
with Session(engine) as db:
 prefix='users/'+str(uid)+'/'
 for batch in s.client.get_paginator('list_objects_v2').paginate(Bucket=s.bucket,Prefix=prefix):
  for item in batch.get('Contents',[]): s.delete(item['Key'])
 db.execute(delete(User).where(User.id==uid)); db.commit()
 print(json.dumps({{'db_user_removed': db.get(User,uid) is None, 'objects_remaining': bool(s.client.list_objects_v2(Bucket=s.bucket,Prefix=prefix).get('Contents'))}}))
'''), flush=True)
    client.close()