"""Opt-in destructive worker test; touches only a newly registered disposable user.

Run against the local Compose stack. Evidence contains IDs/counts, never credentials.
Lease expiry is an explicit DB transition after SIGKILL, not a ten-minute sleep.
"""

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

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / "recovery-acceptance-evidence.json"


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Docker operation failed (output suppressed)")
    return result.stdout


def remote(code):
    result = subprocess.run(
        ["docker", "exec", "-i", "recap-studio-backend-1", "python", "-"],
        input=code, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("Remote assertion failed (output suppressed)")
    return json.loads(result.stdout)


PRELUDE = """
import json
from uuid import UUID
from datetime import timedelta
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from app.jobs import job_engine, get_job_queue
from app.models import User, Chapter, ChapterFile, Job, Page, IngestionAttempt
from app.ingestion_service import (utcnow, recover_attempt, cleanup_attempt,
    attempt_prefix, owned, finalize, Fenced, ACTIVE)
from app.storage import get_storage
engine=job_engine(); store=get_storage()
"""


def snapshot(chapter):
    return remote(PRELUDE + f"""
with Session(engine) as db:
 ch=db.get(Chapter,UUID('{chapter}'))
 job=db.scalar(select(Job).where(Job.chapter_id==ch.id))
 source=db.get(ChapterFile,job.file_id)
 attempts=db.scalars(select(IngestionAttempt).where(IngestionAttempt.chapter_id==ch.id)).all()
 pages=db.scalars(select(Page).where(Page.chapter_id==ch.id)).all()
 print(json.dumps(dict(chapter_id=str(ch.id), chapter_state=ch.status,
  job_id=str(job.id), job_state=job.status, current_attempt_id=str(job.current_attempt_id),
  original_key=source.storage_key, original_exists=store.exists(source.storage_key),
  pages=len(pages), duplicate_pages=len(pages)-len(set(p.page_number for p in pages)),
  numbers=sorted(p.page_number for p in pages),
  all_current=all(p.attempt_id==job.current_attempt_id for p in pages),
  all_objects_exist=all(store.exists(k) for p in pages for k in (p.storage_key,p.thumbnail_key)),
  all_scoped=all('/attempts/'+str(job.current_attempt_id)+'/' in k
                 for p in pages for k in (p.storage_key,p.thumbnail_key)),
  attempts=[dict(id=str(a.id),state=a.status,cleanup=a.cleanup_status,
   objects=len(store.list_prefix(attempt_prefix(db,a)))) for a in attempts])))
""")


def wait(predicate, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise TimeoutError("Acceptance condition not reached")


def main():
    evidence = {}
    user = None
    client = httpx.Client(base_url="http://localhost:8001/api/v1", timeout=120,
                         headers={"origin": "http://localhost:3000"})

    def request(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        assert response.is_success, (method, path, response.status_code)
        return response.json()

    try:
        # Refuse to interrupt unrelated active ingestion.
        assert remote(PRELUDE + """
with Session(engine) as db:
 print(json.dumps(not bool(db.scalar(select(IngestionAttempt.id).where(IngestionAttempt.status.in_(ACTIVE))))))
"""), "Unrelated active ingestion exists"
        user = request("POST", "/auth/register", json={
            "email": f"recovery-{uuid.uuid4().hex}@example.com",
            "password": secrets.token_urlsafe(32),
        })["id"]
        client.headers["x-csrf-token"] = client.cookies["recap_csrf"]
        project = request("POST", "/projects", json={"name": "Disposable recovery acceptance"})
        chapter = request("POST", f"/projects/{project['id']}/chapters",
                          json={"name": "Synthetic 200 pages"})["id"]
        image = io.BytesIO()
        Image.new("RGB", (1800, 2400), "teal").save(image, "PNG")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            for n in range(1, 201):
                z.writestr(f"{n:03}.png", image.getvalue())
        docker("stop", "recap-studio-worker-1")
        request("POST", f"/chapters/{chapter}/upload", files={
            "file": ("synthetic.zip", archive.getvalue(), "application/zip")})
        queued = snapshot(chapter)
        evidence["queued"] = queued
        assert queued["job_state"] == "queued" and queued["original_exists"]
        old = queued["current_attempt_id"]
        job = queued["job_id"]
        docker("start", "recap-studio-worker-1")

        def partial():
            s = snapshot(chapter)
            assert s["chapter_state"] not in {"failed", "ready"}
            return s if 2 <= s["attempts"][0]["objects"] < 400 else None

        evidence["before_kill"] = wait(partial)
        docker("kill", "--signal=KILL", "recap-studio-worker-1")
        killed = snapshot(chapter)
        evidence["after_kill"] = killed
        assert killed["pages"] == 0 and killed["duplicate_pages"] == 0
        assert killed["chapter_state"] != "ready" and killed["job_state"] != "completed"
        assert killed["original_exists"] and 0 < killed["attempts"][0]["objects"] < 400
        remote(PRELUDE + f"""
with Session(engine) as db:
 a=db.get(IngestionAttempt,UUID('{old}'))
 a.lease_expires_at=utcnow()-timedelta(seconds=1); db.commit()
print(json.dumps(True))
""")
        # Stale state survives an actual backend restart.
        docker("restart", "recap-studio-backend-1")
        wait(lambda: docker("inspect", "--format", "{{.State.Health.Status}}",
                            "recap-studio-backend-1").strip() == "healthy")
        retry = request("POST", f"/chapters/{chapter}/processing-retry")
        new = retry["attempt_id"]
        assert new != old
        evidence["recovered"] = snapshot(chapter)
        evidence["fencing"] = remote(PRELUDE + f"""
results={{}}
for name in ('page_insert','chapter_update','job_success'):
 try:
  with owned(engine,UUID('{job}'),UUID('{old}')) as (db,j,a):
   raise AssertionError('stale ownership accepted')
 except Fenced: results[name]='fenced before mutation'
try:
 finalize(engine,UUID('{job}'),UUID('{old}'),[],store)
 raise AssertionError('stale finalize accepted')
except Fenced: results['finalize']='fenced'
cleanup_attempt(UUID('{old}'))
results['duplicate_recovery_same_id']=str(recover_attempt(UUID('{job}'),UUID('{old}')))=='{new}'
print(json.dumps(results))
""")
        docker("restart", "recap-studio-redis-1")
        docker("start", "recap-studio-worker-1")

        def complete():
            s = snapshot(chapter)
            assert s["chapter_state"] != "failed"
            return s if s["chapter_state"] == "ready" else None

        final = wait(complete, 300)
        assert final["pages"] == 200 and final["numbers"] == list(range(1, 201))
        assert final["duplicate_pages"] == 0 and final["job_state"] == "completed"
        assert all(final[k] for k in ("original_exists", "all_current", "all_objects_exist", "all_scoped"))
        assert {a["id"]: a["objects"] for a in final["attempts"]} == {old: 0, new: 400}
        evidence["final"] = final
        request("POST", f"/chapters/{chapter}/processing-cleanup")
        request("POST", f"/chapters/{chapter}/processing-cleanup")
        docker("restart", "recap-studio-worker-1", "recap-studio-backend-1")
        wait(lambda: docker("inspect", "--format", "{{.State.Health.Status}}",
                            "recap-studio-backend-1").strip() == "healthy")
        evidence["after_completed_restart"] = snapshot(chapter)
        assert evidence["after_completed_restart"] == final
        evidence["result"] = "PASS for this scenario only"
    except Exception as exc:
        evidence["result"] = "FAIL"
        evidence["error_type"] = type(exc).__name__
        raise
    finally:
        if user:
            # Stop the disposable task before removing its exact user namespace.
            docker("stop", "recap-studio-worker-1")
            evidence["cleanup"] = remote(PRELUDE + f"""
prefix='users/{user}/'
for key in store.list_prefix(prefix): store.delete(key)
with Session(engine) as db:
 db.execute(delete(User).where(User.id==UUID('{user}'))); db.commit()
 assert db.get(User,UUID('{user}')) is None
print(json.dumps(dict(user_removed=True, objects_remaining=len(store.list_prefix(prefix)))))
""")
            docker("start", "recap-studio-worker-1")
        client.close()
        EVIDENCE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()