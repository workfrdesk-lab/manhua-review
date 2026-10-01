"""Single natural-lease diagnostic, only in disposable infrastructure."""
import hashlib
import io
import json
import os
from pathlib import Path
import time
import zipfile
from datetime import datetime, timezone
from uuid import uuid4

import boto3
import httpx
from PIL import Image
from redis.exceptions import ConnectionError as RedisConnectionError
from kombu.exceptions import OperationalError
from sqlalchemy import text

from app.config import get_settings
from app.jobs import job_engine
from app.worker import celery_app
from app.ingestion_service import ACTIVE, expired, LEASE_SECONDS

OUT = Path('/evidence')


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    assert os.environ.get('DIAGNOSTIC_ISOLATED') == 'yes'
    settings = get_settings()
    assert '@postgres:5432/diagnostic' in settings.database_url.get_secret_value()
    assert settings.s3_bucket == 'diagnostic-private'
    engine = job_engine()
    store = boto3.client('s3', endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name=settings.s3_region)
    client = httpx.Client(base_url='http://backend:8000', timeout=120,
                         headers={'origin': 'http://localhost:3000'})
    ev = {'ingestion_recovery': 'OPEN', 'classification': 'unresolved',
          'http': [], 'snapshots': [], 'signals': [], 'lease_samples': [],
          'started_at': now(), 'lease_seconds': LEASE_SECONDS}

    def save():
        tmp = OUT / 'checkpoint.tmp'
        tmp.write_text(json.dumps(ev, default=str, indent=2), encoding='utf-8')
        tmp.replace(OUT / 'checkpoint.json')

    def signal(name):
        ev['signals'].append({'name': name, 'requested_at': now()})
        save()
        (OUT / (name + '.request')).write_text(now(), encoding='utf-8')
        deadline = time.monotonic() + 180
        while not (OUT / (name + '.ack')).exists():
            assert time.monotonic() < deadline, 'host signal timeout'
            time.sleep(.2)
        ev['signals'][-1]['acknowledged_at'] = now()

    def request(path, **kwargs):
        started = now()
        r = client.post('/api/v1' + path, **kwargs)
        body = r.json()
        # Persist only known public API fields; never cookies, tokens or passwords.
        allowed = {'id', 'job_id', 'chapter_id', 'attempt_id', 'status', 'error', 'success'}
        ev['http'].append({'path': path, 'started_at': started, 'received_at': now(),
            'status': r.status_code, 'request_id': r.headers.get('x-request-id'),
            'body': {k: v for k, v in body.items() if k in allowed}})
        save()
        return r

    def state():
        result = {'started_at': now()}
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as db:
            with db.begin():
                db.execute(text('SET TRANSACTION READ ONLY'))
                result['database_time'] = db.scalar(text('SELECT clock_timestamp()'))
                for table in ('chapters', 'jobs', 'ingestion_attempts', 'chapter_files', 'pages'):
                    column = 'id' if table == 'chapters' else 'chapter_id'
                    result[table] = [dict(r) for r in db.execute(text(
                        f'SELECT * FROM {table} WHERE {column} = :id ORDER BY id'),
                        {'id': chapter}).mappings()]
        result['db_snapshot_finished_at'] = now()
        return result

    def snapshot(label, hashed=False):
        s = state()
        s['label'] = label
        s['lease_owner'] = {'value': None, 'reason': 'not_present_in_schema'}
        s['heartbeat_at_mapping'] = 'ingestion_attempts.last_heartbeat_at'
        s['objects'] = []
        for page in store.get_paginator('list_objects_v2').paginate(Bucket=settings.s3_bucket):
            for obj in page.get('Contents', []):
                item = {k: obj[k] for k in ('Key', 'Size', 'ETag', 'LastModified')}
                if hashed:
                    response = store.get_object(Bucket=settings.s3_bucket, Key=obj['Key'])
                    with response['Body'] as stream:
                        item['sha256'] = hashlib.sha256(stream.read()).hexdigest()
                s['objects'].append(item)
        s['inventory_finished_at'] = now()
        ev['snapshots'].append(s)
        save()
        return s

    def ready():
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if client.get('/health/live').status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(1)
        raise AssertionError('API readiness timeout')

    def worker_ready():
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                stats = celery_app.control.inspect(timeout=3).stats()
                registered = celery_app.control.inspect(timeout=3).registered() if stats else None
                queues = celery_app.control.inspect(timeout=3).active_queues() if stats else None
            except (RedisConnectionError, OperationalError, OSError) as exc:
                ev.setdefault('readiness_connection_errors', []).append({
                    'at': now(), 'type': type(exc).__name__})
                save()
                time.sleep(1)
                continue
            if stats:
                assert all(v['pool']['max-concurrency'] == 2 for v in stats.values())
                assert celery_app.conf.task_acks_late is False
                assert celery_app.conf.worker_prefetch_multiplier == 1
                ev.setdefault('readiness_observations', []).append({'at': now(), 'stats': stats, 'registered': registered, 'queues': queues})
                save()
                if (registered and all('ingestion.process' in tasks for tasks in registered.values())
                        and queues and all(any(q['name'] == 'system' for q in entries) for entries in queues.values())):
                    return stats
            time.sleep(1)
        raise AssertionError('worker readiness timeout')

    def validate(s):
        assert len(s['jobs']) == len(s['chapters']) == len(s['chapter_files']) == 1
        job = s['jobs'][0]
        attempts = s['ingestion_attempts']
        current = [a for a in attempts if a['id'] == job['current_attempt_id']]
        assert len(attempts) == 2 and len(current) == 1
        assert current[0]['generation'] == 2 and current[0]['status'] == 'completed'
        assert sum(a['status'] == 'completed' for a in attempts) == 1
        old = next(a for a in attempts if a['generation'] == 1)
        assert old['status'] == 'abandoned' and old['cleanup_status'] == 'swept'
        assert job['status'] == 'completed' and job['error'] is None
        assert s['chapters'][0]['status'] == 'ready'
        assert len(s['pages']) == 100
        assert sorted(p['page_number'] for p in s['pages']) == list(range(1, 101))
        assert all(p['attempt_id'] == current[0]['id'] and p['status'] == 'ready' for p in s['pages'])
        expected = {s['chapter_files'][0]['storage_key']}
        for p in s['pages']:
            expected.update((p['storage_key'], p['thumbnail_key']))
            inventory = {o['Key']: o for o in s['objects']}
            assert inventory[p['storage_key']]['sha256'] == ev['expected_page_sha256']
            assert inventory[p['thumbnail_key']]['sha256'] == ev['expected_thumbnail_sha256']
        assert len(expected) == 201
        assert {o['Key'] for o in s['objects']} == expected
        assert len(s['objects']) == len(expected)
        assert all(o['Size'] > 0 and o.get('sha256') for o in s['objects'])
        source = next(o for o in s['objects'] if o['Key'] == s['chapter_files'][0]['storage_key'])
        assert source['sha256'] == ev['source_sha256']
        return True

    try:
        ready()
        ev['worker_before'] = worker_ready()
        ev['worker_config'] = {'acks_late': False, 'prefetch': 1, 'concurrency': 2}
        r = request('/auth/register', json={'email': f'stale-{uuid4().hex}@example.com',
            'password': uuid4().hex + 'Aa!', 'display_name': 'Synthetic stale diagnostic'})
        r.raise_for_status()
        client.headers['x-csrf-token'] = client.cookies['recap_csrf']
        r = request('/projects', json={'name': 'Disposable natural stale recovery'})
        r.raise_for_status()
        project = r.json()['id']
        r = request(f'/projects/{project}/chapters', json={'name': 'Synthetic 100 page archive'})
        r.raise_for_status()
        chapter = r.json()['id']
        ev['chapter_id'] = chapter
        image = io.BytesIO()
        Image.new('RGB', (2048, 2048), (30, 120, 180)).save(image, format='PNG')
        with Image.open(io.BytesIO(image.getvalue())) as source:
            rgb = source.convert('RGB')
            output = io.BytesIO()
            rgb.save(output, format='JPEG', quality=92)
            ev['expected_page_sha256'] = hashlib.sha256(output.getvalue()).hexdigest()
            rgb.thumbnail((320, 480))
            output = io.BytesIO()
            rgb.save(output, format='JPEG', quality=80)
            ev['expected_thumbnail_sha256'] = hashlib.sha256(output.getvalue()).hexdigest()
            rgb.close()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as z:
            for i in range(100):
                z.writestr(f'page-{i+1:03}.png', image.getvalue())
        ev['source_sha256'] = hashlib.sha256(archive.getvalue()).hexdigest()
        r = request(f'/chapters/{chapter}/upload',
            files={'file': ('synthetic.zip', archive.getvalue(), 'application/zip')})
        r.raise_for_status()
        deadline = time.monotonic() + 120
        while True:
            s = state()
            a = s['ingestion_attempts'][0]
            objects = store.list_objects_v2(Bucket=settings.s3_bucket).get('Contents', [])
            if a['status'] == 'processing_pages' and len(objects) >= 3:
                break
            assert a['status'] != 'completed', 'missed interruption window; no rerun'
            assert time.monotonic() < deadline, 'processing timeout'
            time.sleep(.05)
        snapshot('before_real_worker_kill')
        signal('kill')
        s = snapshot('after_real_worker_kill')
        old = s['ingestion_attempts'][0]
        assert old['status'] in ACTIVE and old['started_at'] is not None
        assert old['generation'] == 1
        ev['real_interruption'] = True
        deadline = time.monotonic() + 660
        while True:
            s = state()
            a = s['ingestion_attempts'][0]
            assert a == old, 'orphan attempt changed while worker stopped'
            ev['lease_samples'].append({'observed_at': now(), 'database_time': s['database_time'],
                'lease_expires_at': a['lease_expires_at'], 'last_heartbeat_at': a['last_heartbeat_at'],
                'status': a['status']})
            save()
            if expired(a['lease_expires_at']) and s['database_time'] > a['lease_expires_at']:
                break
            assert time.monotonic() < deadline, 'natural expiry timeout'
            time.sleep(10)
        ev['natural_lease_expiry'] = True
        ev['recoverable_basis'] = 'Current ACTIVE attempt with naturally expired lease; production expired() returned true; recover_attempt lines 270-285'
        snapshot('naturally_expired_before_retry')
        signal('start')
        ev['worker_replacement'] = worker_ready()
        ev['replacement_ready_before_dispatch_at'] = now()
        r = request(f'/chapters/{chapter}/processing-retry')
        s = snapshot('immediate_after_retry')
        ev['http_409'] = r.status_code == 409
        r.raise_for_status()
        current = next(a for a in s['ingestion_attempts'] if a['id'] == s['jobs'][0]['current_attempt_id'])
        assert current['generation'] == 2 and str(current['id']) == r.json()['attempt_id']
        ev['stale_recovery_succeeded'] = True
        ev['replacement_attempt_id'] = str(current['id'])
        deadline = time.monotonic() + 300
        while True:
            s = state()
            a = next(a for a in s['ingestion_attempts'] if str(a['id']) == ev['replacement_attempt_id'])
            ev.setdefault('replacement_execution_samples', []).append({
                'observed_at': now(), 'status': a['status'], 'started_at': a['started_at'],
                'finished_at': a['finished_at'], 'last_heartbeat_at': a['last_heartbeat_at']})
            if s['jobs'][0]['status'] == 'completed':
                break
            assert s['jobs'][0]['status'] != 'failed', 'replacement failed'
            assert time.monotonic() < deadline, 'replacement timeout'
            time.sleep(1)
        ev['replacement_completed'] = True
        before = snapshot('completed_before_restart', hashed=True)
        ev['db_aistor_consistent'] = validate(before)
        for service in ('worker', 'backend', 'redis'):
            signal('restart-' + service)
            ready()
            ev['worker_after_' + service + '_restart'] = worker_ready()
            time.sleep(5)
            after = snapshot('after_' + service + '_restart', hashed=True)
            validate(after)
            for key in ('chapters', 'jobs', 'ingestion_attempts', 'chapter_files', 'pages', 'objects'):
                assert before[key] == after[key], 'persistence mismatch: ' + key
        ev['restart_persistence_verified'] = True
        ev['classification'] = 'single-natural-stale-recovery-passed-not-final-acceptance'
    except Exception as exc:
        ev['classification'] = 'unresolved'
        ev['error_type'] = type(exc).__name__
        # Assertion messages are harness-owned; never persist provider exceptions.
        ev['error_detail'] = str(exc) if isinstance(exc, AssertionError) else 'provider detail withheld'
    finally:
        ev['finished_at'] = now()
        save()
        (OUT / 'done').write_text(now(), encoding='utf-8')
        client.close()
        engine.dispose()


if __name__ == '__main__':
    main()