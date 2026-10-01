# Worker crash recovery decision

**Scope:** Phase 4A infrastructure validation only. Phase 4B is **NOT STARTED**.

## Observed real-service failure

The disposable run in `D:\projects\manhua review\focused_worker_crash.py` used the
real API, Celery worker, PostgreSQL, Redis, and MinIO/AIStor with a generated
200-page ZIP. The worker container was killed after the job entered
`processing_pages`, then restarted. The bounded post-restart observation was:

| Observation | Result |
|---|---|
| Job status | `processing_pages` |
| Chapter status | `processing_pages` |
| PostgreSQL page rows | `0` |
| Page numbers | empty |
| Original uploaded object | present |
| Objects under the chapter prefix | `9` |
| Referenced objects | `1` (the original upload) |
| Unreferenced objects | `8` |
| Page objects | none represented by database rows |
| Explicit `submit(job_id)` after restart | accepted by the queue API |
| State after the additional bounded wait | still `processing_pages`, with zero page rows |

The run cleaned its disposable user and storage prefix after collecting these
observations. The captured value-free output is in
`D:\projects\manhua review\focused-worker-crash.log`.

This is a characterization record, not a production behavior change. In
particular, no late acknowledgements, automatic retries, or delivery changes
were enabled.

## Existing recovery mechanism review

No safe administrator/API recovery mechanism exists for this failure:

- `Job` has `status`, `error`, `created_at`, and `updated_at`, but no lease or
  stale-job query/recovery operation.
- The ingestion status endpoint is an owner-scoped read endpoint only.
- `LocalJobQueue.cancel()` changes only `queued` jobs; it cannot cancel or
  reset a processing job.
- `CeleryJobQueue.submit()` sends the same job ID, but `process_job()` claims
  only `Job.status == "queued"`; redispatching a stuck `processing_pages` job
  therefore returns without processing it.
- There is no endpoint or administrative command to mark this job failed,
  reset it to queued, or delete objects by chapter prefix.
- Existing storage deletion handles known page objects and upload-failure
  cleanup. It does not discover and clean unreferenced objects.

The explicit redispatch observation above confirms that the existing queue
entry point is not a recovery mechanism for this state.

## Recovery strategy comparison

| Strategy | Crash behavior | Duplicate execution / DB interaction | S3 orphan risk | Guarantee / operations |
|---|---|---|---|---|
| `acks_late=True` plus idempotency | An unacknowledged task becomes eligible for redelivery after worker loss. | Redelivery is possible; page writes and object publication need an idempotent design and a durable claim/finalization protocol. | Still exists if a process dies between object writes and DB commit; idempotency reduces duplicate rows, not every orphan window. | Stronger automatic recovery, but invasive and operationally harder. It is explicitly deferred. |
| Explicit stale-job watchdog/reaper | A bounded-age `processing_pages` job is marked failed (and optionally its known prefix is cleaned), then an operator explicitly requeues a controlled retry. | No broker redelivery. Requeue must be serialized against a still-running worker and use a fresh/controlled attempt; DB state transitions are explicit. | A reaper can enumerate the chapter prefix and remove objects not referenced by committed rows, subject to a conservative grace period. | Smallest change that makes the current failure visible and recoverable, but requires watchdog scheduling and operator/runbook discipline. |
| Both | Broker redelivery plus stale-job intervention. | Highest duplicate/concurrency complexity; both mechanisms must coordinate claims, transactions, and attempts. | Better coverage only after idempotent cleanup/publication is designed; does not remove orphan windows by itself. | Most resilient long term, not the smallest safe first fix. |
| Another minimal design | Not recommended before the two mechanisms above are evaluated. | A separate attempt/lease and object-manifest protocol would be a larger redesign. | Could improve guarantees, but expands schema and storage changes. | Out of scope for this decision. |

### Recommendation

The smallest safe production fix is an **explicit stale-job watchdog/reaper**,
implemented with conservative age/lease rules, an atomic stale-state
transition, cleanup of only objects proven to belong to the stale attempt, and
an explicit requeue operation. It should be designed and tested before enabling
any automatic redelivery. This recommendation is not implemented in this
task.

## Secret audit status

The audit is value-free: it reports only PASS/FAIL and category/location. The
previous captured audit identified FAIL categories at working-tree/test-output
and frontend build/source, and FAIL for decoded image-layer coverage. Those
FAILs are not silently converted to PASS. Docker is unavailable in the current
environment, so a fresh service, image-history, filesystem-layer, and runtime
credential audit cannot be completed here. Compressed layer coverage must be
reported as FAIL/UNVERIFIED unless every layer is decoded; no license contents
or secret values are printed.

## Final status

1. MinIO/AIStor: **PASS** for the prior real-stack health/run; orphan cleanup behavior is the observed blocker.
2. PostgreSQL: **PASS** for the prior real-stack run; zero page rows remained after the crash.
3. Redis: **PASS** for the prior real-stack worker run.
4. Celery: **PASS** for baseline delivery; crash recovery is not automatic and no delivery semantics were changed.
5. Ingestion: **FAIL** for worker-crash recovery; job remains stuck and leaves unreferenced objects.
6. Worker crash recovery: **FAIL**; no safe existing stale-job/requeue/orphan-cleanup mechanism.
7. Secret audit: **FAIL / INCOMPLETE** pending rerun with Docker; prior value-free results include working-tree/test-output, frontend build/source, and decoded image-layer coverage failures.

### Recovery decision

Current worker-crash behavior **requires a production fix before Phase 4B**.
The evidence is a stuck job, zero committed pages, and eight unreferenced
objects after a real 200-page ZIP worker termination. This is not acceptable to
carry forward based on optimism.

### Phase 4B

**NOT STARTED**