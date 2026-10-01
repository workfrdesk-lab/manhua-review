"""Disposable-container probe. Never run against an existing application stack."""
import io
import json
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

import boto3
import httpx
from PIL import Image
from sqlalchemy import text

from app.config import get_settings
from app.jobs import job_engine
from app.worker import celery_app


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    assert os.environ.get("DIAGNOSTIC_ISOLATED") == "yes"
    settings = get_settings()
    assert "@postgres:5432/diagnostic" in settings.database_url.get_secret_value()
    assert settings.s3_bucket.startswith("diagnostic-")
    engine = job_engine()
    storage = boto3.client(
        "s3", endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name=settings.s3_region,
    )
    storage.head_bucket(Bucket=settings.s3_bucket)
    evidence = {"http": [], "snapshots": [], "classification": "unresolved",
                "ingestion_recovery": "OPEN", "historical_cause": "not established"}
    client = httpx.Client(base_url="http://backend:8000", timeout=60,
                          headers={"origin": "http://localhost:3000"})

    def request(method, path, **kwargs):
        started = now()
        response = client.request(method, "/api/v1" + path, **kwargs)
        evidence["http"].append({"method": method, "path": "/api/v1" + path,
            "started_at": started, "received_at": now(), "status": response.status_code,
            "request_id": response.headers.get("x-request-id"), "body": response.json()})
        return response

    def snapshot(label, chapter):
        result = {"label": label, "started_at": now()}
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            with db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
                result["database_time"] = str(db.scalar(text("SELECT clock_timestamp()")))
                for table in ("chapters", "jobs", "ingestion_attempts", "chapter_files", "pages"):
                    column = "id" if table == "chapters" else "chapter_id"
                    result[table] = [dict(row) for row in db.execute(text(
                        f"SELECT * FROM {table} WHERE {column} = :chapter"),
                        {"chapter": chapter}).mappings()]
        result["lease_owner"] = {"value": None, "reason": "not_present_in_schema"}
        result["heartbeat_at_mapping"] = "ingestion_attempts.last_heartbeat_at"
        result["db_snapshot_finished_at"] = now()
        result["objects"] = []
        for page in storage.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket):
            result["objects"].extend({k: item[k] for k in ("Key", "Size", "ETag", "LastModified")}
                                     for item in page.get("Contents", []))
        result["inventory_finished_at"] = now()
        evidence["snapshots"].append(result)
        return result

    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                if client.get("/health/live").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "API readiness timeout"
            time.sleep(1)
        deadline = time.monotonic() + 60
        stats = None
        while not stats and time.monotonic() < deadline:
            stats = celery_app.control.inspect(timeout=3).stats()
            if not stats:
                time.sleep(1)
        evidence["worker_config"] = {
            "acks_late": celery_app.conf.task_acks_late,
            "prefetch": celery_app.conf.worker_prefetch_multiplier,
            "queue": celery_app.conf.task_default_queue,
            "result_backend": celery_app.conf.result_backend,
            "ignore_result": celery_app.conf.task_ignore_result,
            "stats_before": stats,
            "queues": celery_app.control.inspect(timeout=5).active_queues(),
        }
        assert evidence["worker_config"]["acks_late"] is False
        assert evidence["worker_config"]["prefetch"] == 1
        stats = evidence["worker_config"]["stats_before"]
        assert stats and all(v["pool"]["max-concurrency"] == 2 for v in stats.values())
        r = request("POST", "/auth/register", json={"email": f"diagnostic-{uuid4().hex}@example.com",
                    "password": uuid4().hex + "Aa!", "display_name": "Synthetic diagnostic"})
        r.raise_for_status()
        client.headers["x-csrf-token"] = client.cookies["recap_csrf"]
        r = request("POST", "/projects", json={"name": "Isolated 409 diagnostic"})
        r.raise_for_status()
        project = r.json()["id"]
        r = request("POST", f"/projects/{project}/chapters", json={"name": "Synthetic PNG"})
        r.raise_for_status()
        chapter = r.json()["id"]
        data = io.BytesIO()
        Image.new("RGB", (64, 64), (30, 120, 180)).save(data, format="PNG")
        r = request("POST", f"/chapters/{chapter}/upload",
                    files={"file": ("synthetic.png", data.getvalue(), "image/png")})
        r.raise_for_status()
        deadline = time.monotonic() + 120
        while True:
            state = snapshot("waiting_for_natural_completion", chapter)
            if state["ingestion_attempts"] and state["ingestion_attempts"][0]["status"] == "completed":
                break
            assert time.monotonic() < deadline, "Natural completion timeout"
            time.sleep(1)
        evidence["pre_retry"] = state
        r = request("POST", f"/chapters/{chapter}/processing-retry")
        # First operation after response: read-only DB snapshot, then storage inventory.
        post = snapshot("immediate_after_retry", chapter)
        evidence["reproduced_409"] = r.status_code == 409
        evidence["celery_after"] = {
            "active": celery_app.control.inspect(timeout=3).active(),
            "reserved": celery_app.control.inspect(timeout=3).reserved(),
        }
        message = r.json().get("error", {}).get("message")
        if r.status_code == 409 and message == "Attempt is not recoverable":
            before_attempt = state["ingestion_attempts"][0]
            after_attempt = post["ingestion_attempts"][0]
            assert before_attempt == after_attempt
            assert state["jobs"] == post["jobs"]
            assert before_attempt["status"] == "completed"
            assert state["jobs"][0]["current_attempt_id"] == before_attempt["id"]
            evidence["classification"] = "expected-application-behavior"
            evidence["exact_branch"] = {
                "service": "app/ingestion_service.py:268-269: attempt.status == completed -> raise Fenced()",
                "http": "app/ingestion.py:233-234: except Fenced -> HTTPException(409, Attempt is not recoverable)",
                "basis": "Matching response message plus stable completed current attempt before and after request; not status-code-only inference",
            }
        elif r.status_code != 409:
            evidence["result"] = "historical 409 not reproduced under valid test conditions"
        evidence["race"] = "No race claimed; completion observed before retry was sent."
        evidence["scope"] = "Completed-attempt retry control; does not reproduce or explain historical stale-recovery sequence."
    except Exception as exc:
        evidence["probe_error_type"] = type(exc).__name__
    finally:
        print("DIAGNOSTIC_RESULT=" + json.dumps(evidence, default=str), flush=True)
        engine.dispose()
        client.close()


if __name__ == "__main__":
    main()