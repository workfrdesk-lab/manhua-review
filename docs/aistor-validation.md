# AIStor local validation — 2026-09-30

MINIO STATUS: **PARTIAL** — worker-interruption failure and audit coverage gaps; details
are listed below. Phase 4B remains closed.

## Stable-image replacement

The previous Enterprise-only hotfix was removed from Compose. The current stable
AIStor release is:

```text
quay.io/minio/aistor/minio:RELEASE.2026-09-19T17-05-25Z
sha256:107cf2014a9583c74c11e3cdbd6903d89c2244355886b89ce532f4ecae9c23f3
```

Official sources:

- Stable release feed: https://dl.min.io/api/releases/aistor/latest
- AIStor release artifacts: https://docs.min.io/aistor/operations/release-artifacts/
- AIStor licenses / Free support: https://docs.min.io/aistor/operations/licenses/
- License inspection: https://docs.min.io/aistor/reference/cli/mc-license/mc-license-info/

MinIO documents AIStor Free support beginning with
`RELEASE.2025-12-20T04-58-37Z`; the selected stable release is newer and has no
`.hotfix.*` suffix. The tag was pulled successfully from `quay.io` before use.

## Runtime setup

Compose pins the stable image shown above by digest
`sha256:107cf2014a9583c74c11e3cdbd6903d89c2244355886b89ce532f4ecae9c23f3`.
The supplied file was located outside the workspace at
`C:\Users\MSC\Downloads\minio.license`; `/mnt/data/minio.license` was not accessible.
No license was copied into the workspace.

Set the path in each PowerShell session before running Compose:

```powershell
Set-Location 'D:\projects\manhua review'
$env:MINIO_LICENSE_PATH = 'C:\Users\MSC\Downloads\minio.license'
docker compose up -d --build storage storage-init
```

Compose mounts the file read-only at `/run/secrets/minio.license` only on storage.
The server consumes it using `--license /run/secrets/minio.license`, following
https://docs.min.io/aistor/installation/container/install/ . Local Compose file
secrets are runtime file mounts, not an encrypted secret store. Protect the host
file with host filesystem permissions. Never paste its contents into commands,
configuration, logs, or the repository. Do not delete the storage volume.

Bucket initialization uses the existing backend image's boto3 dependency, creates
`recap-private` if absent, deletes its bucket policy to remove anonymous access,
and checks bucket existence. Neither legacy MinIO image is used.

## Observed results

- AIStor healthy; live and ready endpoints returned HTTP 200.
- Application readiness initially reported storage down because the health probe
  incorrectly used the boto3 S3 client as a context manager. The probe now uses
  `contextlib.closing`; regression tests cover both successful and failing probes.
  After rebuilding backend/worker, application readiness returned HTTP 200 with
  `status: ready` and all four services up.
- Startup reports `License: MinIO Community License`; no S3-denial warning seen.
- Initializer exited 0; real S3 bucket listing and HEAD succeeded.
- Unchanged application S3 provider: PUT, GET, HEAD/exists, overwrite, DELETE,
  missing object, object listing, and invalid-bucket 404 passed.
- An object survived a storage restart; subsequent deletion was verified.
- Real HTTP PNG upload passed through PostgreSQL, Redis/Celery, and AIStor.
  Job completed, chapter became ready, page and thumbnail records/objects existed,
  source bytes matched, and thumbnail decoded successfully.
- Duplicate dispatch of the completed job did not create another page.
- Stable-image HTTP ingestion was repeated with PNG, multi-page ZIP, and multi-page
  PDF fixtures. PostgreSQL job/page state, exact original bytes, page and thumbnail
  objects, thumbnail decoding, scoped user/project/chapter keys, ready status, and
  duplicate-dispatch idempotency passed for each fixture.
- The temporary validation user, project, chapters, database rows, and all objects
  under its user prefix were removed after the run.
- Real API isolation passed with two users, three projects, and separate chapters:
  cross-user project/chapter/page reads, deletes, updates, uploads, reorder, and
  processing-status access returned HTTP 404; project/chapter listings contained
  only owned resources; private bucket listing returned HTTP 403. Same-user
  project and chapter separation also passed. Keys remained scoped under
  `users/{user}/projects/{project}/chapters/{chapter}/`.
- Application persistence passed after restarting backend, worker, AIStor, and
  Redis: PNG, ZIP, and PDF projects/chapters/pages/order/status, originals, page
  objects, thumbnail objects, and readable thumbnails remained available.
- Redis restart/reconnect passed; a new real PNG ingestion completed after Redis
  and the worker recovered.
- Controlled AIStor outage before worker execution produced `failed` with zero
  pages. Restoring storage left the job failed; explicit redispatch did not retry
  it. This is the current early-ack/no-retry behavior and was not changed.
- Restarting AIStor around a 200-page ZIP ingestion completed with 200 ready pages.
  Final consistency checks showed no missing referenced objects and no duplicate
  page numbers. The disposable user cleanup removed all associated objects.
- License-pattern scans passed without printing secret values: working tree, documentation,
  env/config, test output, filesystem, container inspect/environment, logs, API and
  sampled frontend responses, Compose output, Docker history, and saved image archives.
  This is not a complete all-credential audit or proof of decoded compressed-layer
  coverage. The exact running frontend filesystem was not exported and scanned.
  License mount isolation passed; only storage had the read-only license mount.
- Git metadata unavailable in this workspace.
- Earlier-image evidence only: real stopped storage produced EndpointConnectionError.
  This outage probe has not been repeated against the stable image. Only this probe used
  shortened boto3 timeouts/retries; application/Celery semantics were unchanged.
- Storage availability returned after restart. Gracefully restarted worker and
  Redis recovered sufficiently for a Celery control ping. This does not prove
  in-flight task recovery or queue durability under abrupt termination.
- Compose config validation and backend Ruff checks passed.
- Final full backend pytest suite passed: `501 passed, 10 warnings` (399.56 seconds).
- Targeted health regression tests passed: `8 passed`; Ruff checks passed.
- Earlier validation's license exact-byte scan of container logs and edited Compose/ignore files was
  clean. Docker inspect confirmed read-only mount isolation from application and
  frontend containers. No license was supplied to any image build.

## Remaining acceptance gaps

- The Enterprise-only hotfix concern was resolved by replacing that image with
  the stable tag/digest above. Recorded safe license metadata was `Plan: FREE`,
  `Expiry: N/A`, with no trial indicator. Hotfix restrictions are documented at
  https://docs.min.io/aistor/operations/hotfixes/ and do not describe the current pin.
- Git metadata unavailable in this workspace. This is not an acceptance blocker.
- The pytest suite uses local-storage/local-processing fixtures by default;
  the separate real-service experiments above supply integration evidence.
- **Incomplete test — comprehensive secret leakage audit:** license-pattern scans
  passed within their stated scope, but all-credential, decoded compressed-layer,
  and exact running frontend filesystem coverage was not established. This is a
  validation coverage limitation, not evidence of a leak. It blocks full acceptance
  and Phase 4B until the requested audit is completed.
- **Failed test — real in-flight worker interruption:** after terminating the real
  worker during a 200-page ZIP ingestion and restarting it, the chapter remained
  `processing_pages` with zero committed page rows. It did not report success and
  had no missing referenced objects, but it was not converted to a truthful
  terminal failure and the interrupted run temporarily left two unreferenced
  objects before disposable-user cleanup. This is a real current limitation in
  worker/job recovery, not a validation-tool failure. It blocks `MINIO STATUS:
  VERIFIED` and therefore blocks Phase 4B acceptance.
- The earlier validation project `ee938174-a679-4292-b32f-e22eaf72626e` was
  confirmed to be validation-only and removed. Its database records and scoped
  objects were deleted and the prefix was verified empty.

No storage interfaces, key scheme, Story architecture, or Celery semantics changed.