# Phase 7C Section 14 final evidence pass — 2026-10-02

## Decision: PASS / implementation complete

This pass changed only `D:\projects\manhua review\backend\tests\test_script_handoff.py`
and this report. No production code or `phase7c-spec.md` was changed. No commit or
push was made.

The four requested test-evidence gaps have been strengthened and pass. Explicit
implementation authorization for Phase 7C is present in the current session:
implement Phase 7C according to the finalized specification, preserve 7A/7B, run
the release gates, and do not commit or push automatically. Gate 14.1 therefore
passes for this Phase 7C implementation.

## PostgreSQL and cleanup

One disposable PostgreSQL container was used sequentially:

| Item | Value |
| --- | --- |
| container | `phase7c-final-evidence-pg` |
| container ID | `fcaa2ab1fd029bf8f5dc7c5f88d68733811412d7c276717e270d6f471a0f63b0` |
| image / digest | `postgres:16` / `sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54` |
| server | PostgreSQL 16.15, `127.0.0.1:55437` |
| named volume | `phase7c-final-evidence-data` |

The container was stopped/removed with `docker rm -f phase7c-final-evidence-pg`,
and the named volume was removed with `docker volume rm phase7c-final-evidence-data`.
Verification showed neither remained. No test-run pytest process remained; IDE/helper
Python processes were not touched.

The earlier `phase7c-release-pg` container is absent. Its removal without `-v`
predates this pass. Thirteen dangling anonymous volumes were listed, but there is
no retained mount ID linking one to that deleted container. None was removed:
unattached does not mean owned by this task. This is a historical cleanup-verification
limitation, not a Phase 7C implementation failure. This run created no anonymous
volume.

Server query: PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2), x86_64-pc-linux-gnu,
gcc (Debian 14.2.0-19) 14.2.0, 64-bit. The recorded sha256 is Docker's image ID.

## Closed evidence gaps

- **Ordering:** three distinct panel/OCR rows were repeatedly deleted/reinserted in
  different heap/UUID orders; the differing query orders were proved, then full
  handoff content was asserted identically in validated sequence `Middle, Zulu, Alpha`.
- **Concurrency:** the 3-before/3-after structure now contains three distinct
  segments, sequences, narration texts, and evidence. Events coordinate six
  transactions; every full response is compared for dependency, all segments,
  ordering, evidence, and eligibility.
- **Preloaded caller:** the caller preloads ScriptVersion, StoryVersion, Page, Panel,
  OCRResult, and User. A separate writer changes OCR after preload. The fresh
  service Repeatable Read/read-only snapshot observes `source_invalid`, while the
  caller's OCR identity-map object remains unchanged.
- **No side effects/errors:** all 24 existing state/language/source cases (including
  eligible controls) compare all
  mapped persistent tables, local storage bytes, and queue history. Injected errors
  at the HTTP service boundary and validation boundary assert the exact sanitized
  500, `no-store`, no ETag, and unchanged state.

## Sequential commands and results

Environment for each PostgreSQL command:

```powershell
$env:TEST_DATABASE_URL='postgresql://<redacted>@127.0.0.1:55437/gate'
$env:ALLOW_TEST_DB_RESET='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
```

1. `pytest -q tests/test_script_handoff.py --tb=short --maxfail=1` via the
   Windows Selector event-loop wrapper: **52 passed, 0 failed, 0 skipped, 1 warning,
   55.70s**.
2. Targeted command over `test_script.py`, `test_script_revision.py`,
   `test_script_finalization.py`, `test_script_postgresql.py`, `test_script_handoff.py`,
   `test_story_validation.py`, `test_story_closure.py`, `test_story_resolution.py`:
   **157 passed, 0 failed, 0 skipped, 2 warnings, 128.30s**.
3. Full backend `pytest -q --tb=short`: **623 passed, 0 failed, 0 skipped, 11
   warnings, 521.42s**.
4. `ruff check backend/app backend/tests`: **PASS**.
5. `python -m compileall -q backend/app backend/tests`: **PASS**.
6. `git diff --check`: **PASS**.

After the full suite, an additional explicit Story/Integrity run passed **11 passed,
0 failed, 0 skipped, 2 warnings, 7.08s**. Static checks were repeated afterward.
Warnings comprise Starlette/httpx deprecation, Pydantic enum serialization, and
the existing migration foreign-key cycle warning.

Intermediate results, not final evidence: initial `py_compile` passed; initial Ruff
reported one import-formatting error, corrected test-only. The first focused run
was **39 passed, 1 failed, 0 skipped** (44.26s): the new fixture incorrectly used
null front matter instead of schema-required strings. Corrected to empty strings
before the authoritative 52/157/623 runs. Earlier session parallel DB-reset runs
remain invalid evidence; no concurrent suites were used in this pass.

### Exact validation commands

Working directory: `D:\projects\manhua review\backend`, environment above.
Relative test paths below are pytest arguments relative to that absolute directory.

```powershell
& 'D:\projects\manhua review\.venv\Scripts\python.exe' -u -c "import asyncio,pytest; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); raise SystemExit(pytest.main(['-q','tests/test_script_handoff.py','--tb=short','--maxfail=1']))"
& 'D:\projects\manhua review\.venv\Scripts\python.exe' -u -c "import asyncio,pytest; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); raise SystemExit(pytest.main(['-q','--tb=short','tests/test_script.py','tests/test_script_revision.py','tests/test_script_finalization.py','tests/test_script_postgresql.py','tests/test_script_handoff.py','tests/test_story_validation.py','tests/test_story_closure.py','tests/test_story_resolution.py']))"
& 'D:\projects\manhua review\.venv\Scripts\python.exe' -u -c "import asyncio,pytest; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); raise SystemExit(pytest.main(['-q','--tb=short']))"
& 'D:\projects\manhua review\.venv\Scripts\python.exe' -u -c "import asyncio,pytest; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); raise SystemExit(pytest.main(['-q','--tb=short','tests/test_story.py','tests/test_story_integrity.py']))"
& 'D:\projects\manhua review\.venv\Scripts\ruff.exe' check 'D:\projects\manhua review\backend\app' 'D:\projects\manhua review\backend\tests'
& 'D:\projects\manhua review\.venv\Scripts\python.exe' -m compileall -q 'D:\projects\manhua review\backend\app' 'D:\projects\manhua review\backend\tests'
git diff --check
```

### Container commands

```powershell
docker run --name phase7c-final-evidence-pg --label phase7c.final-evidence=true -e POSTGRES_USER=<redacted> -e POSTGRES_PASSWORD=<redacted> -e POSTGRES_DB=gate -p 127.0.0.1:55437:5432 -v phase7c-final-evidence-data:/var/lib/postgresql/data -d postgres:16
docker inspect phase7c-final-evidence-pg --format '{{.Id}} {{.Config.Image}} {{.Image}} {{json .Mounts}}'
docker exec phase7c-final-evidence-pg pg_isready -U gate -d gate
docker exec phase7c-final-evidence-pg psql -U gate -d gate -Atc 'select version()'
docker rm -f phase7c-final-evidence-pg
docker volume rm phase7c-final-evidence-data
docker ps -a --filter name='^phase7c-final-evidence-pg$' --format '{{.Names}}'
docker volume ls --filter name='^phase7c-final-evidence-data$' --format '{{.Name}}'
docker volume ls --filter dangling=true --format '{{.Name}}'
Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' } | Select-Object ProcessId,CommandLine
```

Initial immediate readiness probe returned no response; the next readiness poll
succeeded. Cleanup commands succeeded; exact-name listings returned no matches.
Only VS Code Python helpers remained. No process-kill command was used.

The commands were run one at a time, never in parallel.

## Section 14 gate table

| Gate | Status | Evidence / remaining gap |
| --- | --- | --- |
| 14.1 Authorization | **PASS** | The current session explicitly authorizes implementing Phase 7C only according to the finalized specification, preserving 7A/7B, running release gates, and making no automatic commit/push. |
| 14.2 Contract/service | **PASS** | Exact fields/nullability, durations, all reason combinations, parity, validation, and deliberate three-row ordering pass. |
| 14.3 Security/HTTP | **PASS** | Auth/ownership/non-disclosure, malformed input, sanitized 500, no-store, no ETag, and persistent no-side-effect checks pass. |
| 14.4 Database/concurrency | **PASS** | Real PostgreSQL, Repeatable Read/read-only verification, Events, six readers, multi-segment coherence, identity-map isolation, and no writes pass. |
| 14.5 Regression/repository | **PASS** | 157 targeted, 11 explicit Story/Integrity, and 623 full tests pass; static checks and the final run's cleanup pass. The anonymous volume from an earlier already-removed container cannot be safely identified from retained records; this historical cleanup-verification limitation is explicitly not treated as a Phase 7C implementation failure. |

Phase 7C implementation is technically complete. All final release gates for the
current run passed. The historical anonymous-volume limitation is documented above
and does not change the release decision. The earlier report also requested explicit
mapping of broader dialogue/nullability and binding acceptance evidence; this pass
relies on existing contract/validation/regression coverage and does not add a new
dialogue acceptance scenario.

## Scope verification

Only these files were edited by this final pass:

```text
D:\projects\manhua review\backend\tests\test_script_handoff.py
D:\projects\manhua review\docs\phase7c-release-evidence.md
```

Pre-existing working-tree changes to production and specification files remain
untouched: `backend/app/main.py`, `backend/app/script.py`,
`backend/app/script_handoff.py`, and `docs/phase7c-spec.md`.

All top-level production Python files and the specification were SHA256-compared
against a baseline captured before edits: **0 hash differences**. Untracked test
and report files are not included by normal `git diff --check`; Ruff/compileall
and direct file reads additionally verified the test file.

No commit or push has been performed. The working tree is not clean: intentional
uncommitted Phase 7C implementation, test, and evidence files remain pending human
review.