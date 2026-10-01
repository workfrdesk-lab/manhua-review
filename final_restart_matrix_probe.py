"""One isolated Seven-Scenario Restart Matrix probe; lifecycle is host-orchestrated."""
import hashlib
import io
import json
import os
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import boto3
import httpx
from redis.exceptions import ConnectionError as RedisConnectionError
from kombu.exceptions import OperationalError
from sqlalchemy import text
from PIL import Image

from app.config import get_settings
from app.ingestion_service import LEASE_SECONDS, expired
from app.jobs import job_engine
from app.worker import celery_app

OUT = Path("/evidence")
SCENARIO = os.environ["MATRIX_SCENARIO"]


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    assert os.environ.get("MATRIX_ISOLATED") == "yes"
    settings = get_settings()
    engine = job_engine()
    store = boto3.client("s3", endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(), region_name=settings.s3_region)
    assert '@postgres:5432/matrix' in settings.database_url.get_secret_value()
    assert settings.s3_bucket == 'matrix-private'
    celery = celery_app
    client = httpx.Client(base_url="http://backend:8000", timeout=120,
                          headers={"origin": "http://localhost:3000"})
    ev = {"scenario_id": SCENARIO, "started_at": now(), "lifecycle": [], "snapshots": [],
          "signals": [], "worker_semantics": {"acks_late": False, "prefetch": 1,
          "concurrency": 2, "queue": "system"}, "result": "OPEN"}

    def save():
        (OUT / "checkpoint.json.tmp").write_text(json.dumps(ev, indent=2, default=str), encoding="utf-8")
        (OUT / "checkpoint.json.tmp").replace(OUT / "checkpoint.json")

    def signal(name):
        row = {"name": name, "requested_at": now()}
        ev["signals"].append(row); save()
        (OUT / (name + ".request")).write_text(row["requested_at"], encoding="ascii")
        deadline = time.monotonic() + 300
        ack = OUT / (name + ".ack")
        while not ack.exists():
            assert time.monotonic() < deadline, "host lifecycle timeout: " + name
            time.sleep(.2)
        row["acknowledged_at"] = now(); save()

    def request(method, path, **kwargs):
        started = now(); response = client.request(method, "/api/v1" + path, **kwargs)
        body = response.json() if response.content else {}
        ev.setdefault("http", []).append({"method": method, "path": path, "started_at": started,
            "received_at": now(), "status": response.status_code,
            "body": {k: body[k] for k in ("id", "job_id", "chapter_id", "attempt_id", "status") if k in body}})
        return response

    def state(label, hashes=False):
        result = {"label": label, "captured_at": now()}
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            with db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
                result['database_time'] = db.scalar(text('SELECT clock_timestamp()'))
                for table in ("chapters", "jobs", "ingestion_attempts", "chapter_files", "pages"):
                    column = "id" if table == "chapters" else "chapter_id"
                    result[table] = [dict(row) for row in db.execute(text(
                        f"SELECT * FROM {table} WHERE {column}=:id ORDER BY id"), {"id": ev["chapter_id"]}).mappings()]
        result["objects"] = []
        for page in store.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket):
            for obj in page.get("Contents", []):
                item = {k: obj[k] for k in ("Key", "Size", "ETag", "LastModified")}
                if hashes:
                    item["sha256"] = hashlib.sha256(store.get_object(Bucket=settings.s3_bucket,
                        Key=obj["Key"])["Body"].read()).hexdigest()
                result["objects"].append(item)
        if label not in ('execution_window', 'completion_wait'):
            ev["snapshots"].append(result); save()
        return result

    def ready(timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if client.get("/health/live").status_code == 200: return
            except httpx.TransportError: pass
            time.sleep(1)
        raise AssertionError("backend readiness timeout")

    def worker_ready():
        assert celery.conf.task_acks_late is False
        assert celery.conf.worker_prefetch_multiplier == 1
        assert celery.conf.task_default_queue == 'system'
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                inspect = celery.control.inspect(timeout=3)
                stats, registered, queues = inspect.stats(), inspect.registered(), inspect.active_queues()
                if stats and registered and queues:
                    assert all(v["pool"]["max-concurrency"] == 2 for v in stats.values())
                    assert all("ingestion.process" in tasks for tasks in registered.values())
                    assert all(any(q["name"] == "system" for q in qs) for qs in queues.values())
                    ev["worker_readiness"] = {"at": now(), "stats": stats, "registered": registered, "queues": queues}
                    save(); return
            except (OSError, RedisConnectionError, OperationalError):
                pass
            time.sleep(1)
        raise AssertionError("worker readiness timeout")

    def validate(final):
        assert len(final['jobs']) == len(final['chapters']) == len(final['chapter_files']) == 1
        job = final['jobs'][0]
        attempts = final['ingestion_attempts']
        current = next(a for a in attempts if a['id'] == job['current_attempt_id'])
        assert current['status'] == job['status'] == 'completed'
        assert current['started_at'] and current['finished_at']
        assert final['chapters'][0]['status'] == 'ready'
        assert len(final['pages']) == 100
        assert sorted(p['page_number'] for p in final['pages']) == list(range(1, 101))
        assert all(p['attempt_id'] == current['id'] and p['status'] == 'ready' for p in final['pages'])
        expected = {final['chapter_files'][0]['storage_key']}
        inventory = {o['Key']: o for o in final['objects']}
        for p in final['pages']:
            expected.update((p['storage_key'], p['thumbnail_key']))
            assert inventory[p['storage_key']]['sha256'] == ev['expected_page_sha256']
            assert inventory[p['thumbnail_key']]['sha256'] == ev['expected_thumbnail_sha256']
            assert '/attempts/' + str(current['id']) + '/' in p['storage_key']
        assert len(expected) == len(final['objects']) == 201
        assert set(inventory) == expected
        assert inventory[final['chapter_files'][0]['storage_key']]['sha256'] == ev['source_sha256']
        for a in attempts:
            if a['id'] != current['id']:
                assert a['status'] == 'abandoned' and a['cleanup_status'] == 'swept'
        if int(SCENARIO) in (4, 5, 6):
            assert len(attempts) == 2 and current['generation'] == 2
        return True

    def recover():
        before = state('before_recovery')
        retry = request('POST', f'/chapters/{chapter}/processing-retry')
        after = state('immediate_after_recovery')
        ev['recovery_response'] = {'status': retry.status_code, 'body': retry.json()}
        if retry.status_code == 409:
            ev['409_evidence'] = {'before': before, 'after': after,
                'active': celery.control.inspect(timeout=3).active(),
                'reserved': celery.control.inspect(timeout=3).reserved(),
                'branch': 'completed -> Fenced' if any(a['status'] == 'completed' and a['id'] == before['jobs'][0]['current_attempt_id'] for a in before['ingestion_attempts']) else 'unresolved; see state and response'}
            save()
        retry.raise_for_status()
        ev['replacement_attempt_id'] = str(after['jobs'][0]['current_attempt_id'])
        assert ev['replacement_attempt_id'] != ev['attempt_id']

    def complete():
        deadline = time.monotonic() + LEASE_SECONDS + 360
        while time.monotonic() < deadline:
            s = state('completion_wait')
            a = next(a for a in s['ingestion_attempts'] if a['id'] == s['jobs'][0]['current_attempt_id'])
            if s['jobs'][0]['status'] == 'completed':
                return
            assert s['jobs'][0]['status'] != 'failed', 'application ingestion failed'
            if expired(a['lease_expires_at']) and s['database_time'] > a['lease_expires_at']:
                state('naturally_expired'); recover()
            time.sleep(1)
        raise AssertionError('completion timeout')

    try:
        ready(); worker_ready()
        email = f"matrix-{uuid4().hex}@example.com"; password = uuid4().hex + "Aa!"
        response = request("POST", "/auth/register", json={"email": email, "password": password})
        response.raise_for_status(); client.headers["x-csrf-token"] = client.cookies["recap_csrf"]
        project = request("POST", "/projects", json={"name": "Disposable matrix " + SCENARIO},
                         headers={"x-csrf-token": client.cookies["recap_csrf"]}).json()["id"]
        chapter = request("POST", f"/projects/{project}/chapters", json={"name": "New fixture " + SCENARIO},
                          headers={"x-csrf-token": client.cookies["recap_csrf"]}).json()["id"]
        ev["chapter_id"] = chapter
        image = io.BytesIO(); Image.new("RGB", (2048, 2048), (30, 120, 180)).save(image, "PNG")
        with Image.open(io.BytesIO(image.getvalue())) as im:
            rgb = im.convert('RGB'); output = io.BytesIO(); rgb.save(output, 'JPEG', quality=92)
            ev['expected_page_sha256'] = hashlib.sha256(output.getvalue()).hexdigest()
            rgb.thumbnail((320, 480)); output = io.BytesIO(); rgb.save(output, 'JPEG', quality=80)
            ev['expected_thumbnail_sha256'] = hashlib.sha256(output.getvalue()).hexdigest()
        image_bytes = image.getvalue(); archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as z:
            for i in range(100): z.writestr(f"page-{i + 1:03}.png", image_bytes)
        source_hash = hashlib.sha256(archive.getvalue()).hexdigest(); ev["source_sha256"] = source_hash
        upload = request("POST", f"/chapters/{chapter}/upload",
                         files={"file": ("new-fixture.zip", archive.getvalue(), "application/zip")},
                         headers={"x-csrf-token": client.cookies["recap_csrf"]}); upload.raise_for_status()
        initial = state("after_upload")
        ev["job_id"] = str(initial["jobs"][0]["id"]); ev["attempt_id"] = str(initial["ingestion_attempts"][0]["id"])
        ev["generation"] = initial["ingestion_attempts"][0]["generation"]
        # Wait until real execution has begun; no artificial lease/DB transition is used.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current = state("execution_window")
            attempt = current["ingestion_attempts"][0]
            if attempt["status"] == 'processing_pages' and len(current['objects']) >= 3:
                break
            assert attempt['status'] != 'completed', 'harness missed active interruption window'
            time.sleep(.5)
        else: raise AssertionError("real ingestion did not start")
        ev["receipt_start_evidence"] = {"observed_at": now(), "attempt": attempt}
        state('before_intervention')
        stale = int(SCENARIO) in (4, 5, 6)
        if stale:
            signal("kill-worker")
            stopped = state('after_kill')['ingestion_attempts'][0]
            assert stopped['status'] == 'processing_pages'
            deadline = time.monotonic() + LEASE_SECONDS + 90
            while time.monotonic() < deadline:
                current = state("natural_lease_wait")
                old = current["ingestion_attempts"][0]
                assert old == stopped, 'orphan attempt changed while worker stopped'
                if expired(old["lease_expires_at"]) and current['database_time'] > old['lease_expires_at']: break
                time.sleep(10)
            else: raise AssertionError("natural lease expiry timeout")
            ev["natural_stale"] = {"observed_at": now(), "attempt": old}
        if SCENARIO != '7':
            service = ('worker', 'backend', 'redis')[(int(SCENARIO) - 1) % 3]
            signal("restart-" + service)
            if stale and service != 'worker': signal('start-worker')
            ready(); worker_ready()
        if stale:
            recover()
        complete()
        if SCENARIO == "7":
            before = state('completion_before_restarts', hashes=True); validate(before)
            for service in ("worker", "backend", "redis"):
                signal("restart-" + service); ready(); worker_ready()
                after = state('after_' + service + '_restart', hashes=True); validate(after)
                for key in ('chapters', 'jobs', 'ingestion_attempts', 'chapter_files', 'pages', 'objects'):
                    assert before[key] == after[key], 'persistence mismatch: ' + key
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            final = state("final", hashes=True)
            if final["jobs"][0]["status"] == "completed": break
            if final["jobs"][0]["status"] == "failed": raise AssertionError("ingestion failed")
            time.sleep(1)
        else: raise AssertionError("completion timeout")
        job = final["jobs"][0]; current_id = str(job["current_attempt_id"])
        pages = final["pages"]; keys = {obj["Key"] for obj in final["objects"]}
        current_pages = [p for p in pages if str(p["attempt_id"]) == current_id]
        ev["completion_evidence"] = {"observed_at": now(), "job_status": job["status"],
            "current_attempt_id": current_id, "page_count": len(pages), "current_page_count": len(current_pages)}
        ev["final_consistency"] = len(pages) == 100 and len(current_pages) == 100 and \
            len({p["page_number"] for p in pages}) == 100 and all(p["status"] == "ready" for p in pages) and \
            all(k in keys for p in pages for k in (p["storage_key"], p["thumbnail_key"])) and \
            job["status"] == "completed" and final["chapters"][0]["status"] == "ready"
        ev['final_consistency'] = validate(final)
        ev['duplicate_object_check'] = True
        ev['stale_generation_cleanup'] = True
        ev["result"] = "PASS" if ev["final_consistency"] else "FAIL"
    except Exception as exc:
        ev["result"] = "FAIL"; ev["failure_type"] = type(exc).__name__; ev["failure"] = str(exc)
    finally:
        ev["finished_at"] = now(); save(); (OUT / "done").write_text(ev["finished_at"], encoding="ascii")
        client.close(); engine.dispose()


if __name__ == "__main__": main()