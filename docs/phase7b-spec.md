# Phase 7B specification: script language, revision, approval, and invalidation

## Status and authority

**Resolved specification; awaiting explicit approval of this revision. Implementation is NOT authorized.**

Phase 7A is CLOSED at `58c6df82a3831125ecbe9ca251562710dc6bc444`.
This closure reference comes from the task context; the commit object is present
locally. The supplied workspace HEAD is `b5881ad67c1d5cb6a0312bf82f3bbd95c816abec`
(`initial commit`), not that closure commit. Baseline observations below refer to
the inspected working tree; this document does not assert HEAD equals 7A closure
or authorize changing the checkout.
This document defines the resolved Phase 7B contract, not a claim that its additions
already exist. Section 16 records the resolution of OPEN-01 through OPEN-07.
No policy decision remains open. The legacy refinement and complete revised
specification still require explicit user approval before any implementation.

Only this document is changed by this planning task. No migration is created.

Repository root: `d:\projects\manhua review`.
Baseline references (absolute paths):

- `d:\projects\manhua review\docs\architecture.md`: Phase 7 covers Arabic/English,
  editing, approval, and version invalidation; approval precedes future TTS.
- `d:\projects\manhua review\backend\app\script.py`: ownership, generation,
  metadata/segment editing, review, job status, and retry APIs.
- `d:\projects\manhua review\backend\app\script_schemas.py`: typed profiles,
  segments, and evidence references.
- `d:\projects\manhua review\backend\app\script_validation.py`: exact snapshot
  grounding, chronological order, quotes, and confidence limits.
- `d:\projects\manhua review\backend\app\models.py`: ScriptVersion,
  ScriptSegment, ScriptEvidence, ScriptGenerationJob, and StoryAudit constraints.
- `d:\projects\manhua review\backend\app\script_service.py`: persisted attempts,
  atomic publication, timeout recovery, and stale-worker fence.
- `d:\projects\manhua review\backend\app\jobs.py` and
  `d:\projects\manhua review\backend\app\worker.py`: existing queue boundary.
- `d:\projects\manhua review\backend\migrations\versions\0013_script_domain.py`,
  `d:\projects\manhua review\backend\migrations\versions\0014_script_jobs.py`,
  and `d:\projects\manhua review\backend\migrations\versions\0015_script_evidence_scope.py`.
- `d:\projects\manhua review\frontend\src\app\chapters\[id]\page.tsx`:
  existing script selection, editing, review, polling, and retry UI.

## 1. Objective and bounded scope

Phase 7A already provides grounded script generation, persisted attempts,
provider adapters, exact StoryVersion binding, editing, human review, audit,
frontend review, and PostgreSQL evidence/source integrity. Do not reimplement it.

Phase 7B proposes these additions inside Phase 7:

1. Explicit Arabic/English selection and visible immutable language metadata.
2. A precise editing contract and complete frontend controls for allowed fields.
3. Approval bound to the actual reviewed script revision, rather than treating
   the ScriptVersion UUID alone as sufficient proof of approval.
4. Persisted revision identity, stale-client write protection, and a deterministic
   dependency-validity contract for future outputs.
5. Audit and regression coverage for those additions.

No output generation beyond scripts, general workflow engine, new queue,
provider framework, collaboration roles, or asset-invalidation service is added.

### Baseline distinctions that must not be hidden

- A StoryVersion is an immutable source snapshot. Script edits must not edit it
  or substitute the current live Story graph.
- A baseline ScriptVersion is **mutable** through editing APIs despite its name.
  Its UUID is not an immutable content revision identity.
- Generation has a unique `(story_version_id, profile_fingerprint)` ScriptVersion
  identity; the provider/model and `script-v1` contract enter generation identity.
- Existing script edits preserve explicit human review status, including
  `confirmed` and `rejected`. Resetting approval after edits is an intentional
  7B policy change approved under OPEN-02, not a Phase 7A regression fix.
- Existing `fingerprint` is generated at publication and is not recomputed by
  edit routes. It MUST NOT be repurposed as a current-content revision token.
- Existing language validation is a bounded free-form string, default `en`, not
  an Arabic/English enum or linguistic verifier.
- Evidence references are not editable through the current segment PATCH.

## 2. User workflow

Resolved lifecycle:

1. The owner selects an exact StoryVersion from the chapter's version list and
   a generation profile with `language = ar | en`. Do not silently select a
   newer StoryVersion after submission.
2. Generation uses the existing provider and persisted job. Queued/running work
   is reused; completed work is reused without overwriting human edits. Failed
   attempts remain visible and are explicitly retryable under baseline rules.
3. A newly published ScriptVersion starts at revision 1, `needs_review`, with
   the exact StoryVersion ID and immutable generation profile visible.
4. The owner edits permitted fields. The server validates the complete candidate
   script against the bound StoryVersion and current source records, then commits
   all representations, revision, review effects, and audit atomically.
5. Review displays text, language, source/evidence, confidence, duration estimates,
   and the precise revision being reviewed. Review does not imply approval.
6. The owner explicitly confirms that revision. The server revalidates grounding
   and accepts approval only against the current concurrency token.
7. A rejected or confirmed script can be revised using the same editing APIs.
   Resolved policy: a material edit returns the entire script and all segments
   to `needs_review`, increments revision, and invalidates prior dependency tokens.
8. A subsequent explicit confirmation approves the new revision, never the old
   one. Reopening or rejection revokes downstream eligibility immediately.
9. Changing language, StoryVersion, or other generation-profile fields is a new
   generation request, not an edit to the existing binding. Existing matching
   completed generation may be reused. Originals are not relabeled or overwritten.
10. Later phases may consume only a current approved dependency token. Phase 7B
    exposes and tests this contract but creates no audio, timeline, or video.

Step 7 uses in-place revision numbers. Immutable forks and history/restore UI
are excluded. Legacy approval adoption is defined in section 6.

## 3. Arabic/English scripting

### Resolved supported contract

- New generation requests support canonical `ar` (Arabic) and `en` (English).
  Preserve the existing default `en` when language is omitted.
- Reject other values, including unrecognized aliases, with 422; do not silently
  normalize them into a different generation fingerprint. Aliases and dialect
  codes are not supported for new generation requests.
- `ScriptVersion.profile.language` remains authoritative; the corresponding job
  retains the submitted profile. No duplicate editable language column is needed.
- The profile language applies to narration, title, hook, intro, and outro.
  Exact OCR dialogue quotations remain in the source language and are explicitly
  presented as source quotations, not translated dialogue.
- Preserve Unicode text, Arabic shaping characters, punctuation, and mixed-script
  proper names. Do not transliterate, translate, or strip text during an edit.
- Language/profile values are immutable for a ScriptVersion. Editing text does
  not infer, replace, or mutate language metadata.
- Requests and provider outputs must still satisfy existing text length, schema,
  evidence, front-matter, ordering, and finite numeric validation.
- Validation is deterministic metadata/schema validation plus explicit
  human language review. It does NOT claim that Unicode character counts prove
  linguistic correctness. No new language-detection dependency is introduced.
- Provider prompts must explicitly request the chosen narration language while
  retaining source-as-data instructions, bounded context, and grounding rules.
  Mocked provider tests assert the language request and metadata preservation;
  they do not certify actual provider translation quality.
- The existing deterministic fixture provider copies event descriptions and uses
  an English template title; it is not a translator. Language-matched fixtures
  can exercise the workflow, but must not be presented as proof of bilingual
  generation. Production-language quality requires human evaluation of the
  configured generation provider; no translation service is implied.

Automatic linguistic validation and dialect support are excluded. Persisted
profiles retain the baseline free-form language representation for reading,
editing, review validation, and existing job retry/publication. Restrict language
at the new generation-request boundary, not in a way that prevents loading old
profiles. Existing jobs retain their stored profile and baseline retry semantics;
any resulting unsupported-language script is readable but dependency-ineligible.
Do not rewrite profiles or hashes. Exact-quote rules must never be relaxed to
make translated dialogue appear source-authentic.

## 4. Script editing contract

All writes are owner-only, CSRF-protected, transactionally validated, and subject
to the concurrency contract. Unknown fields remain forbidden.

| Field | Resolved edit rule |
| --- | --- |
| `narration_text` | Editable; nonempty, at most 5000 characters; structurally grounded paraphrase requires human review. |
| `dialogue_text` | Editable or removable with the coherent dialogue fields; nonempty when present, at most 1000 characters, exact validated OCR quotation only. |
| `source_dialogue`, `segment_type` | Editable together with dialogue text; supported types remain narration/dialogue/transition/intro/outro; dialogue type iff source dialogue. |
| `speaker_ref` | Non-null attribution remains rejected because source speaker attribution is unavailable. Null removes attribution; the complete candidate must remain coherent. |
| `sequence` | Editable to an integer from 1 through the segment count; atomically move and renumber all segments. Preserve nondecreasing source-page order and contiguous numbering. |
| `estimated_duration` | Editable finite seconds, greater than 0 and at most 3600; an estimate, not measured audio duration. |
| `confidence` | Editable finite value in [0, 1], never greater than the applicable cited source-confidence ceiling. Show low-confidence warnings. |
| `title` | Editable, 1–300 characters; does not change language metadata. |
| `hook`, `intro`, `outro` | Editable, at most 5000 characters; empty or exactly a current segment narration. Preserve baseline linked-text updates on narration edits. |
| Evidence objects and `scene_ref`, `event_refs`, page range | Read-only in 7B. Display them fully; reject mutation fields rather than silently ignore them. |
| Script/segment/evidence IDs, temp IDs, chapter ownership, StoryVersion binding, generation fingerprints, provider identity, timestamps | Immutable/server-managed; not PATCH fields. |
| Review `status` | Changed only by explicit review or the approved edit-reset policy; never editable as content. |
| Revision/concurrency/dependency fields | Server-managed; supplied preconditions are not writable values. |

No segment creation/deletion, splitting/merging, bulk script replacement, source
OCR editing, free-form evidence addition, or new speaker attribution is proposed.
These are not implicit consequences of supporting script editing.

### New ScriptVersion versus revision

- Same binding/profile plus text, order, duration, confidence, or metadata edit:
  in-place ScriptVersion revision, not a new generation job.
- Different StoryVersion, language, style, target duration, narration tone, or
  preserve-dialogue setting: use generation with the new profile/binding; never
  mutate an existing ScriptVersion or manufacture a different fingerprint.
- Repeat the same completed generation: reuse it, including its human edits.
  There is no implicit "regenerate and overwrite" or "clone with same profile".
- Revisions are not immutable historical ScriptVersion rows.
  Audit retains before/after changes; no historical-restore
  endpoint or complete snapshot-history UI is promised. Future outputs must
  retain their actual inputs for reproducibility rather than dereferencing a
  mutable script and calling it the old revision.

### Atomicity and no-ops

Validate the complete proposed state before publication. Synchronize script JSON,
normalized segments, metadata, review status, and audit in one transaction.
Reordering includes all affected rows and any front-matter linkage changes.
Invalid candidates leave every representation unchanged.

Effective no-op writes do not increment revision, reset approval, or
add a change audit. Empty edit requests return 422. A stale precondition fails
even when its requested values happen to equal the latest values. Only optional
`dialogue_text` and `speaker_ref` accept explicit null; other edit fields reject
null with 422. Removing dialogue requires a coherent final candidate with
`source_dialogue=false`, a non-dialogue type, and no speaker attribution. Removing
speaker attribution alone need not remove valid source dialogue. Omitted fields
are unchanged. Compare complete candidate content, including linkage/reordering
effects, before deciding whether a write is a no-op. Legacy confirmation adoption
is an approval-binding change, not a no-op (section 6).

## 5. Approval workflow

Keep exactly the canonical states `needs_review`, `confirmed`, and `rejected`.
"Approved" is a UI term for `confirmed`; do not add a fourth persisted state.
Generation-job states remain separate from human review states.

| Current state | Explicit human review transitions | Resolved material edit result |
| --- | --- | --- |
| `needs_review` | confirmed, rejected, or idempotent needs_review | needs_review, new revision |
| `confirmed` | needs_review, rejected, or idempotent confirmed | needs_review, new revision |
| `rejected` | needs_review, confirmed, or idempotent rejected | needs_review, new revision |

- Preserve baseline owner authorization for editing and approval. There is no
  reviewer/admin bypass, delegated reviewer, or two-person approval workflow.
- Confirmed scripts may be edited, but approval is revoked atomically
  by a material edit. Rejected scripts may be revised and submitted for review.
- Explicit review updates the aggregate and every segment state consistently.
  No independent per-segment approval endpoint is introduced.
- Confirming requires full grounding revalidation using the exact bound snapshot
  and current source records. Errors return 422 and never partially confirm.
- Other state transitions remain explicit owner actions. Repeating the current
  state is an idempotent no-op except confirmed-without-binding adoption in
  section 6. A repeated bound confirmation still revalidates grounding; failure
  returns 422 without mutations, and success does not create a new approval epoch.
- Revision increments for each actual review-state transition as well
  as content edit. Approval therefore identifies the post-transition revision.
- No automatic confirmation, worker-generated confirmation, or implicit approval
  because generation completed successfully is permitted.

The edit-reset policy is an approved intentional change from 7A. Canonical
`confirmed` alone never proves dependency eligibility: the current revision must
also have the explicit approval binding defined below.

## 6. Revision and dependency invalidation contract

### Revision identity and explicit approval binding

Add a persisted positive integer `ScriptVersion.revision`, initially 1. Increment
once per material transaction changing content, order, confidence, duration,
metadata, or review state. It is not a timestamp, job attempt, or generation hash.
Do not reset it on retry or roll it back on rejection/reopening.

Add nullable integer `ScriptVersion.approved_revision`. It is server-managed,
not a new review state or historical record. A non-null value MUST equal the
current `revision`, and the canonical status MUST be `confirmed`.

- New scripts: `revision=1`, `approved_revision=null`, `status=needs_review`.
- Material edit: increment revision once, reset aggregate/all segments to
  needs_review, clear approved_revision in the same transaction.
- Actual review transition to confirmed: validate, increment once, set
  approved_revision to the resulting revision, and confirm all segments/JSON.
- Actual review transition to needs_review/rejected: increment once, clear
  approved_revision, and update all segment/JSON statuses.
- Same-state needs_review/rejected, or already-bound confirmed: no revision,
  binding, timestamp, or audit change. Confirmation validation still applies.

### Legacy representation and explicit adoption

Migration sets **every existing script** to `revision=1` and
`approved_revision=null`. The value 1 is a concurrency baseline, not a claim
about historical edit count. Existing canonical script/segment/JSON review
states, content, metadata, timestamps, profile, both fingerprints, StoryVersion
binding, evidence, jobs, and prior audits remain unchanged. No migration-created
review audit or historical approval token is manufactured.

A legacy confirmed script therefore remains `confirmed`, but lacks a
revision-bound approval. No `is_legacy` column or legacy-only state is needed:
`confirmed` plus null approved_revision identifies the unbound approval that
needs explicit adoption. Null on needs_review/rejected merely means unapproved;
there is no need to distinguish its creation era.

The explicit user action is the existing review PATCH with `status=confirmed`
and the current If-Match. When the current status is already confirmed and
approved_revision is null, this is **reconfirmation/adoption**, not a no-op:

1. Authorize, lock and check the current precondition as for every mutation.
2. Revalidate the complete script against its exact snapshot/current sources.
3. Increment revision once (normally 1 -> 2), set approved_revision equal to that
   new revision, and synchronize aggregate/segment/JSON statuses to confirmed.
4. Commit one explicit human `review` audit and return 200 with the new ETag.

No forced reopen, content edit, new script/job, or special adoption endpoint is
required. Invalid grounding returns 422, preserving the old approval and all
data. Concurrent adoption requests using the old ETag yield one success and one
412. Retrying with the new ETag is a validated no-op once the binding exists.
Legacy needs_review/rejected scripts enter the model through normal confirmation.
Editing any legacy script follows the same reset/clear/increment policy.

Legacy non-ar/en scripts remain readable and editable, and may explicitly be
confirmed/reconfirmed using their unchanged stored profile and grounding rules.
They can acquire approved_revision, but remain dependency-ineligible with
`unsupported_language`. Human approval and supported downstream language are
separate checks; confirmation must not relabel them or promise downstream use.

Expose an additive `dependency` object on script reads:

```json
{
  "story_version_id": "uuid",
  "script_version_id": "uuid",
  "revision": 4,
  "language": "ar",
  "profile_fingerprint": "existing-generation-profile-hash",
  "eligible": true,
  "reasons": []
}
```

The immutable comparison token comprises the five identity fields, excluding
`eligible`/`reasons`. Identity fields are exposed even when ineligible; their
presence is not an approval token or authorization to consume the script.
Eligibility requires confirmed status, approved_revision == revision, supported
language, and successful current source/grounding validation. Return all applicable
reasons in lexical order: `review_required` for needs_review, `rejected` for
rejected, `approval_unbound` for confirmed without a current binding,
`source_invalid` for failed grounding, and `unsupported_language` for non-ar/en.
No `story_superseded` reason exists. No media objects or storage keys are included.

Validity for a future consumer requires ALL of:

1. Exact chapter ownership and exact token identity match.
2. Current eligible approval, not merely historical `confirmed` in a cached DTO.
3. Its own provider/settings/input identity matches its persisted inputs.
4. Checks before starting AND immediately before publishing output.

An old output may remain a historical artifact, but MUST NOT be represented as
current valid output after any mismatch. Invalidation is logical, not deletion
of original assets. Reconfirming later must not resurrect an old token.

### Required invalidation matrix

| Trigger | Script effect | Dependent-output effect |
| --- | --- | --- |
| A new StoryVersion is created | Existing script stays bound to the old snapshot; never auto-rebind or rewrite it. | Old-snapshot outputs remain eligible in their own context if all other checks pass; never valid for the new snapshot. |
| User selects a different ScriptVersion | No mutation to either script. | Tokens for the previous selection cannot satisfy the new selection. |
| Segment text, ordering, duration, confidence, or script metadata materially changes | Revision increments, aggregate approval resets. | All outputs depending on the previous script revision are stale, transitively. |
| Language or generation profile changes | New generation identity, not in-place relabeling. | Different profile/language tokens cannot reuse outputs from the previous identity. |
| Evidence mutation requested | API rejects mutation fields with 422; no scene/event/page-range or evidence editing exists. | No mutation or invalidation occurs. |
| Underlying page/panel/OCR changes or disappears | Do not rewrite the StoryVersion; validate against current source records. Preserve DB deletion constraints. | Failed grounding means ineligible even if UUID/revision still match. Future consumers must not rely on token equality alone. |
| confirmed -> needs_review or rejected | Revision increments; approval revoked. | Previous approved token invalid immediately. |
| needs_review/rejected -> confirmed | Revision increments; new approval eligible only after validation. | Creates a new eligible token; does not revive old output validity. |
| Unbound legacy confirmed -> explicitly reconfirmed | Revision increments, current approval binding is established after validation. | First revision-bound approval, not a fabricated historical one; unsupported language still blocks eligibility. |
| No-op edit/review or failed mutation | No revision/state change. | No new invalidation. |
| Job retry or duplicate delivery | Existing completed script unchanged. | No invalidation solely because a job is retried/reused. |

Future dependent outputs include narration/TTS audio and alignment, subtitle
timing derived from that audio/text, script-derived timelines, render previews,
renders, and final video. Invalidate transitively if an output depends on an
invalid input. Source pages and immutable StoryVersions are not dependent outputs
to invalidate or regenerate because a script changed.

Phase 7B scope is the persisted revision, eligibility calculation, API/UI exposure,
and deterministic tests of comparison/invalidation rules. It adds no downstream
tables, processing jobs, audio APIs, timeline APIs, rendering APIs, event bus,
background cascade, or asset deletion. Concrete downstream integration and
source-change/publication locking belong to the consuming phase; it must prove
the above prepublication checks atomically before claiming safe output reuse.

## 7. Idempotency and concurrency

Preserve Phase 7A exactly for generation:

- Profile/provider/model/contract fingerprinting and uniqueness remain intact.
- Concurrent equivalent generation converges on the existing attempt/script.
- Completed reuse never replaces edits, evidence, approval, or revision.
- Retries are only for failed/cancelled jobs; create a fresh attempt and preserve
  its StoryVersion/profile/provider identity. Retry of an older attempt returns
  the newer attempt rather than creating a parallel retry chain.
  This describes the explicit retry endpoint. Preserve the separate baseline
  behavior whereby generation POST can create the next attempt after failure
  when there is no queued/running/completed attempt to reuse.
- A worker claims queued work conditionally; duplicate delivery is harmless.
- Script publication and job completion remain atomic. Expired/failed attempts
  cannot publish after the existing recovery/publication fence.
- Keep the existing fifteen-minute script recovery behavior and sanitized errors.
- No Celery ACK, prefetch, retry, routing, or ingestion-recovery changes.

Resolved editor concurrency:

- Script reads expose `revision` and a strong ETag `"script:<uuid>:<revision>"`.
  List DTOs carry revision; detail/status responses carry the selected script ETag.
- All three mutation routes (segment, metadata, review) require `If-Match` with
  that exact parent ScriptVersion ETag. Missing: 428; stale: 412; malformed: 400.
  Wildcards and multi-value ETags are not accepted.
- Lock the parent script and compare revision inside the same transaction as
  complete validation, write, revision increment, and audit.
- Competing edits or an edit racing approval cannot both succeed against the
  same revision. The loser refetches; never silently merge or approve unseen text.
- Successful writes return the new parent ETag even when the body is a segment.
  Clients refetch detail to observe renumbering and status changes.

Mandatory preconditions intentionally change write-client requirements. Do not
silently deploy them as backward-compatible. Use a coordinated frontend/API
rollout on the existing routes; old clients without If-Match receive 428.
Drain/pause old application writers and workers for migration and switch all
writers to the new revision-aware code before resuming; no mixed-version writes
may bypass counters or binding clearance. This is a release procedure, not a
queue/infrastructure redesign.

## 8. Evidence and source integrity

Preserve the existing ScriptEvidence source chain and PostgreSQL semantics:

- Exact bound snapshot scene/event references, not IDs resolved in a newer graph.
- Evidence sources must already ground the selected snapshot event; every cited
  event must be covered. Rejected scenes/events/characters cannot ground output.
- Page/chapter, panel/page, OCR/panel composite foreign keys remain unchanged.
- Keep non-null evidence page and the rule that OCR evidence requires a panel.
- Preserve script/chapter and segment/script scoping and all source-delete
  outcomes, including ORM/direct SQL/loaded-relationship parity.
- Quotations require matching OCR text; source dialogue must have an exact quote
  and an enabling immutable profile. Do not claim inferred speaker attribution.
- Keep confidence ceilings, source-page ordering, front-matter linkage, and
  complete-output validation. Structural grounding is not semantic entailment.
- Source failures are fail-closed and surfaced, never repaired by invented
  evidence, automatic source replacement, or database-constraint weakening.

## 9. API contract

All paths below include `/api/v1`. Use existing session authorization,
chapter/project ownership, CSRF protection on writes, and existing error-envelope
handling. Anonymous requests receive the existing 401/403 behavior; inaccessible
or missing owned resources return 404. Errors must not leak cross-owner records,
credentials, raw provider exceptions, or source contents beyond authorization.

### Shared DTOs and rules

- **Script row**: preserve existing serialized model fields; add `revision`,
  nullable `approved_revision`, and `dependency`. `profile.language` is
  authoritative. Do not rename/remove existing response fields.
- **Script detail**: script row plus ordered `segments`, top-level `evidence`,
  per-segment evidence and speaker data, preserving existing response structure.
- **Job**: unchanged serialized ScriptGenerationJob fields and lifecycle.
- **Segment row**: unchanged normalized row response for PATCH, with parent ETag
  in response headers. No invented full-detail PATCH response.
- All request schemas forbid unknown fields. UUID, bounds, canonical states,
  unsupported languages, and invalid grounding use 422 except precondition
  errors specified in section 7. Transaction conflicts that cannot safely reuse
  an existing generation retain 409 behavior.

| Method and path | Request | Success response | State restrictions / additional errors |
| --- | --- | --- | --- |
| POST `/chapters/{chapter_id}/scripts` | `{story_version_id: UUID, profile?: ScriptProfile}` | 201 `{job: Job, script: Script row or null}`; preserve baseline reuse and possible null script semantics; poll/refetch by job ID. | Owned StoryVersion in chapter; 404 invalid binding; 503 provider unavailable; 409 unrecoverable generation conflict; queue-dispatch failure remains persisted failed job, not falsely reported completion. |
| GET `/chapters/{chapter_id}/scripts` | None | 200 script-row array, newest first, as baseline | Owner; no review-state restriction. |
| GET `/scripts/{script_id}` | None | 200 Script detail and ETag | Owner; all canonical states readable. |
| GET `/scripts/{script_id}/status` | None | 200 `{script: Script row, job: Job or null}` and script ETag | Owner; all canonical states readable. |
| GET `/chapters/{chapter_id}/script-jobs` | None | 200 Job array | Preserve authorized chapter-scoped recovery behavior; no new background recovery service. |
| GET `/script-jobs/{job_id}/status` | None | 200 Job | Preserve authorized chapter-scoped recovery behavior. |
| POST `/script-jobs/{job_id}/retry` | No body | 202 Job, possibly the already newer attempt | Only failed/cancelled original jobs; 409 otherwise. No edit/review precondition header required for jobs. |
| PATCH `/script-segments/{segment_id}` | Nonempty subset of the editable segment fields in section 4; parent `If-Match` | 200 Segment row plus new parent ETag | Any review state; material edits reset approval; 422 invalid order/coherence/grounding; 400/412/428 preconditions. |
| PATCH `/scripts/{script_id}/metadata` | Nonempty subset `{title?, hook?, intro?, outro?}`; `If-Match` | 200 Script row plus ETag | Any review state; material edits reset approval; 422 invalid/empty metadata or grounding; 400/412/428 preconditions. |
| PATCH `/scripts/{script_id}/review` | `{status: needs_review \| confirmed \| rejected}`; `If-Match` | 200 Script row plus ETag | Transitions in section 5; 422 invalid state or failed confirmation validation; 400/412/428 preconditions. |

The generation profile retains `target_style` (1–80 characters, default
`chronological_recap`), `target_duration_seconds` (15–3600, default 180),
`narration_tone` (1–80, default `neutral`), `preserve_source_dialogue` (default
false), and language as specified in section 3. Do not tighten unrelated profile
choices or change existing fingerprint inputs.

Use the existing GET `/chapters/{chapter_id}/story/versions` to populate source
selection; do not change its contract or add a Story mutation API. Audit is
persisted in the existing StoryAudit domain; no new audit endpoint or frontend
audit-history browser is required by this bounded proposal.

No `/approve`, `/revisions`, `/evidence` mutation, generation cancel, language
PATCH, legacy-adoption, or media endpoint is added. The existing review endpoint
handles explicit legacy reconfirmation.

### Exact legacy API behavior

- Authorized list/detail/status reads return 200 with unchanged old fields plus
  revision, approved_revision, and dependency. An otherwise valid supported-language
  legacy confirmed script returns revision 1, approved_revision null, eligible
  false, reasons `["approval_unbound"]`. Detail/status expose its ETag.
- Invalid grounding or unsupported language affects eligibility, not readability:
  return the stored script with reason codes, not a 422 that hides legacy data.
  Reads must never adopt approvals or update timestamps/counters/audit. Unexpected
  operational failures remain errors, not falsely successful validation.
- PATCH metadata/segment uses the same If-Match/validation/no-op policy as new
  scripts. Successful material edits clear the binding and reset review state;
  failures and no-ops preserve legacy approvals.
- PATCH review with confirmed explicitly adopts an unbound approval per section
  6; supported and unsupported stored languages both allow human confirmation.
  Return the updated Script row including binding/eligibility and the new ETag.
  Review to needs_review/rejected uses normal transitions. No special request flag.
- Authorization/CSRF precede revision comparison. Missing, malformed (including
  weak, wildcard or multi-value), and stale If-Match yield 428, 400, and 412.
  A well-formed ETag for a different script is a nonmatching precondition (412).
  For valid requests, check preconditions before grounding/no-op evaluation.
- New generation POST with non-ar/en returns 422 even if an old matching script
  exists. Existing job retry/status remain available under baseline rules and
  retain stored profiles. Supported completed reuse does not adopt an old
  approval, advance revision, or manufacture a binding.
- Return `Cache-Control: no-store` on script/dependency reads and mutation
  responses; never return 304 using only the script revision. The specified ETag
  is the script write-precondition identity, not proof of freshness for externally
  changing source eligibility or job status. Always recompute eligibility on reads
  and future consumption; source changes need not change this ETag.

## 10. Database and migrations

No migration is applied or created by this document.

A migration with exactly two added ScriptVersion columns IS required:

- Add `script_versions.revision`: non-null integer, server default 1, positive
  check constraint. Backfill existing rows deterministically to 1.
- Add `script_versions.approved_revision`: nullable integer, default NULL.
  Backfill every existing row to NULL, including confirmed rows. Add a check:
  `approved_revision IS NULL OR (approved_revision = revision AND status = 'confirmed')`.
  No history table, adoption flag, approval timestamp column, or new enum.
- Keep all generation uniqueness/fingerprints, StoryVersion foreign keys,
  ScriptEvidence constraints, status values, job attempts, and deletion rules.
- Use existing `StoryAudit.data` JSON for revision and invalidation payloads.
  No audit-column enlargement is required.
- No new language column, output tables, persisted in-memory substitutes,
  downstream invalidation table, or approval enum is needed for this design.
- Preserve all legacy values other than the two new columns as specified in
  section 6. Do not reopen approvals, touch updated_at, rewrite JSON, or add
  synthetic review audits during backfill. Existing indexes/uniqueness stay intact.
- Upgrade/downgrade must be tested with populated scripts, evidence, jobs, and
  audit. Downgrade removes only the added schema; it must not delete scripts or
  pretend to restore historical approval state. Downgrade loses revision-counter
  semantics and is incompatible with active 7B consumers; document that limit.

Do not remove `uq_script_version_profile` or create immutable forks.
The final migration identifier is selected from repository head at implementation.

### Expected implementation surfaces, not permission to edit

- `d:\projects\manhua review\backend\app\script.py`
- `d:\projects\manhua review\backend\app\script_schemas.py`
- `d:\projects\manhua review\backend\app\script_validation.py`
- `d:\projects\manhua review\backend\app\models.py`
- `d:\projects\manhua review\backend\app\story_providers.py` (script prompt only)
- `d:\projects\manhua review\backend\app\script_service.py` (only necessary
  publication/revision integration; no recovery redesign)
- A new approved migration under
  `d:\projects\manhua review\backend\migrations\versions`
- Script tests under `d:\projects\manhua review\backend\tests`
- `d:\projects\manhua review\frontend\src\app\chapters\[id]\page.tsx`
- `d:\projects\manhua review\frontend\src\lib\api.ts` only if needed to expose
  response ETags while preserving existing callers.

Do not refactor Story services, shared worker/queue behavior, storage, or ingestion
as incidental implementation work. Small script-local helpers are acceptable
within the existing modular monolith if justified in the approved implementation plan.

## 11. Frontend review/edit/approval UX

1. Select a specific StoryVersion and Arabic/English before generation; show the
   exact selected ID and chosen profile. No default-to-latest substitution after
   the user has selected a source.
2. Show script ID, bound StoryVersion, language, revision, review state, and
   downstream eligibility/reasons. Distinguish older StoryVersion from invalid
   evidence; do not conflate "not latest" with "corrupt".
3. Keep script/version selection, persisted job progress, failure messages,
   explicit retry, polling, and completed-script reuse. Poll the relevant attempt,
   not an unrelated latest chapter job after the user's selection changes.
4. Arabic narration editors use `lang="ar"`, RTL; English uses `lang="en"`, LTR.
   Isolate IDs/numbers and use suitable direction for mixed/source quotations.
   The application's existing Arabic interface need not become fully bilingual.
5. Expose narration, coherent source dialogue/type controls, order, duration,
   confidence, and title/front matter under section 4. Keep evidence and source
   binding visible and read-only under the resolved scope.
6. Show source page/panel/OCR identifiers, quotes, scene/event references, and
   low-confidence warnings. Existing authorized source viewing is reused.
7. Explicit Save/Cancel; keep unsaved drafts on validation/network/conflict errors.
   Disable approval while unsaved drafts exist; do not silently save on approve.
8. Approve/Reject/Reopen use the current ETag. Warn that saving a material change
   to a confirmed script requires reapproval and invalidates its prior token.
9. On 412, show a conflict notice and offer reload/review; no automatic overwrite
   or resubmission with a fresh ETag. Reload must not silently discard a draft.
10. Refetch after successful mutations so ordering, linked metadata, revision,
    aggregate state, and evidence are consistent. Provide accessible labels,
    keyboard-operable controls, and announced errors.
11. No Play TTS, timeline, render, export video, or fake output controls. Dependency
    status is a contract indicator, not evidence that those stages exist.
12. For confirmed plus approved_revision null, show "Existing approval — explicit
    reconfirmation required for revision-bound use" and a Reconfirm action using
    the normal review PATCH. Do not label the old review rejected or erase it.
    Show unsupported-language warnings independently, including after successful
    reconfirmation. Never imply that reconfirmation changes the language.

## 12. Audit contract

Use existing `StoryAudit`: action at most 30 characters, entity type at most 40,
`version_id` remains the bound StoryVersion ID (never a ScriptVersion ID).
Keep chapter, actor, entity identity, timestamp, and bounded sanitized JSON data.
Owner mutations record the authenticated actor. Preserve job initiator attribution
for worker events and existing foreign-key nulling/deletion behavior.

### Existing actions retained

| Action | Required payload |
| --- | --- |
| `script_generation_requested` | attempt, fingerprint, job_id, story_version_id, script_version_id (null until published) |
| `script_generation_retry` | Same job identity payload; preserve the bounded 7A action name |
| `script_generation_started` | Same job identity payload |
| `script_generation_completed` | Same payload with published script ID; initial revision added |
| `script_generation_failed` | Same identity payload; never raw provider exceptions/credentials |
| `script_segment_edited` | Existing fields/before/after and script/story IDs; add revision_before/after, review_before/after, affected segment IDs and all ordering/front-matter side effects |
| `script_metadata_edited` | Existing before/after and script/story IDs; add changed fields, revision_before/after, review_before/after |
| `review` | Existing before/after states and script/story IDs; add revision_before/after and approval token when confirming |

For edit/review events add `dependency_before`, `dependency_after`, and stable
`invalidation_reason` (`script_edited`, `metadata_edited`, `review_changed`) when
the token changes. This records logical invalidation without inventing future
output IDs or a separate cascade job. A material edit that resets approval records
both effects in the same edit audit, not a misleading human `review` event.

No new event is required merely to read eligibility or discover an invalid source;
the source's own audit domain remains unchanged. Different-language generation
is a normal generation request, not a mutation of the old language.

Audit and mutation commit/rollback together. No successful-change audit for
failed validation, stale writes, no-ops, or rolled-back transactions. Preserve
failed-job audits as lifecycle events.

Every edit/review audit adds `approved_revision_before` and
`approved_revision_after`. Explicit legacy reconfirmation uses one existing
`review` action with before/after `confirmed`, revision_before/after, binding
before null/after new revision, `approval_adopted=true`, actor and script/story
IDs, and dependency_before/after. Use `invalidation_reason=review_changed`.
Other reviews record approval_adopted=false. Record an `approval_token` only
for a newly established current binding, with the five section 6 identity fields;
also record eligibility/reasons so unsupported-language confirmation is not
mistaken for downstream authorization. Never record a token for the prior
unbound legacy approval. The migration itself creates no human review event.

## 13. Deterministic acceptance criteria

These criteria target the resolved contract, including explicit legacy adoption.

1. Generation with `ar`/`en` persists exactly that profile in job and script and
   passes it to both existing provider adapters. Unsupported new language values
   return 422 without dispatch; omission still yields `en`.
2. Arabic Unicode and mixed-script names survive API, persistence, edit, and
   frontend display without normalization loss. Exact OCR quotes are unchanged.
3. Repeating/concurrently submitting the same generation returns the same active
   or completed identity and never overwrites edited content/revision/approval.
4. A valid text/order/duration/confidence/metadata edit updates every affected JSON
   and normalized representation atomically, increments revision exactly once,
   and resets all review states to needs_review under the approved policy.
5. Invalid ranges, nonfinite values, chronological-order violations, invented
   speakers/evidence, unknown fields, broken quotes/front matter, and excess
   confidence fail without changing rows, revision, status, or audit.
6. Empty edits return 422; valid effective no-ops preserve approval/revision and
   create no change audit. Null/removal cases follow the resolved section 4 contract.
7. Every canonical review transition is tested. Repeated bound confirmation is
   a validated no-op; unbound confirmed adoption is the explicit exception.
   Eligibility additionally requires supported language and valid current sources.
8. Confirmed and rejected scripts can be revised. No previously approved token
   becomes valid again after edit/reopen/reject followed by reconfirmation.
9. Two clients writing the same revision yield one successful material mutation
   and one 412, including edit-versus-approve. No lost updates, mixed ordering,
   duplicate revision advancement, or approval of unseen content.
10. Missing/malformed/stale preconditions return 428/400/412 without writes.
    All reads/writes retain cross-owner and anonymous authorization protection.
11. Changing StoryVersion/language/profile never mutates the original binding;
    the existing unique generation identity still governs completed reuse.
12. Dependency-token tests cover every matrix trigger, including changed source
    records with unchanged script revision, unsupported legacy language, and
    version-relative validity with no latest-only supersession. No media services
    are needed for tests.
13. Generation/retry/dispatch failure, duplicate delivery, timeout recovery, stale
    publication, and concurrent generation tests retain 7A guarantees.
14. PostgreSQL rejects cross-chapter/page/panel/OCR evidence chains as before;
    source deletion and ORM/direct SQL/loaded relationship outcomes stay identical.
15. Audit actions fit actual PostgreSQL columns, bind the correct StoryVersion,
    attribute the actor, contain exact before/after/revision effects, and roll
    back with failed mutations. No secret-bearing provider error is stored.
16. Frontend tests/checks cover language direction, exact version selection,
    draft preservation, approval disabling with drafts, conflict handling,
    reapproval warning, polling/retry selection, and read-only evidence.
17. Populated migration upgrade/downgrade preserves existing domain rows and
    constraints. Backfill revision=1/approved_revision=NULL for every old row;
    assert original content, profile, fingerprints, binding, statuses, timestamps,
    evidence and audit are unchanged. Test the new approval-binding DB check.
18. A valid legacy confirmed script is readable with its preserved status, null
    binding and approval_unbound; reads/completed reuse cause no writes. Invalid
    source and unsupported-language cases remain readable with all applicable reasons.
19. Explicit legacy reconfirmation at revision 1 atomically produces revision 2,
    approved_revision 2, unchanged content/profile/fingerprints/binding, synchronized
    confirmed states and exactly one actor-attributed adoption audit. No fake old
    token/audit exists. Repeating with the old ETag returns 412; fresh ETag is a
    validated no-op. Concurrent adoption has exactly one successful writer.
20. Failed legacy confirmation (422), missing/malformed/stale preconditions and
    no-op edits preserve old approvals and all values. Material edits clear binding
    and reset all states; later confirmation binds only the resulting new revision.
21. Non-ar/en legacy scripts allow reads, valid edits, review/reconfirmation and
    existing job retry without relabeling. Even with a current binding they remain
    ineligible with unsupported_language. New generation with that language is 422.
22. Test legacy reconfirmation UX, independent unsupported-language warnings,
    no-store responses and no revision-only 304 optimization. Verify source changes
    alter eligibility without requiring a script write or silently adopting approval.

## 14. Regression and release gates

Before implementation, obtain explicit approval of this revised specification,
and record the exact repository commands/environment for the approved change.
Do not run destructive database tests against an application database.

Required implementation validation:

- Focused Phase 7B deterministic schema/API/service/concurrency/audit tests.
- Phase 7A Script regression, including
  `d:\projects\manhua review\backend\tests\test_script.py`,
  `d:\projects\manhua review\backend\tests\test_script_finalization.py`, and
  `d:\projects\manhua review\backend\tests\test_script_postgresql.py`.
- Relevant Story regression: snapshots, review, evidence, reconciliation,
  version integrity, and authorization remain unchanged.
- Real disposable PostgreSQL tests for every affected DB behavior, concurrency,
  audit width, foreign-key/deletion parity, and migration behavior. Use the
  existing explicit test DB/reset opt-in; never fall back to production settings.
- Populated migration upgrade/downgrade/upgrade if a migration is added.
- Ruff, Python compilation, frontend TypeScript typecheck, frontend production
  build, and `git diff --check`.
- Frontend behavioral tests using the repository's available tooling; record any
  manual browser checks separately and do not present typechecking as UX testing.

Where the approved 7B contract intentionally changes baseline review/no-op/header
behavior, update those specific test expectations with explicit justification;
do not delete coverage or reopen unrelated Phase 7A logic. Preserve all other 7A
assertions. Record exact commands, pass/fail/skip results, environment limits,
and final commit. A skipped PostgreSQL gate is not a passing PostgreSQL gate.
No phase completion claim until relevant gates pass.

## 15. Explicit exclusions

OUT OF SCOPE:

- TTS/audio generation or alignment implementation.
- Timeline generation or editor implementation.
- Rendering, render previews, or final video generation.
- Ingestion recovery changes, Celery delivery-semantics changes.
- Storage/AIStor behavior or configuration changes.
- Credential/license data or configuration changes.
- StoryVersion integrity changes, source-snapshot mutation, live-graph rebinding.
- Provider bypasses, in-memory-only jobs, silent API contract changes.
- New speaker inference, automatic evidence repair, unrelated architecture or
  application refactors, role/collaboration systems, segment split/merge tools.

Documenting future output dependencies does not authorize implementing them.

## 16. Resolved decision register

The original OPEN identifiers are retained only for traceability; all seven
decisions are resolved in this specification. User-approved architectural choices
are distinguished from the fully specified legacy refinement submitted for final
approval with this revision.

| ID | Resolution | Authority |
| --- | --- | --- |
| OPEN-01 — RESOLVED | In-place monotonic ScriptVersion revision; preserve identity/generation uniqueness; no forks or history/restore UI. | User-approved. |
| OPEN-02 — RESOLVED | Every material edit resets aggregate/all segments to needs_review, clears current approval binding and requires explicit reconfirmation. | User-approved. |
| OPEN-03 — RESOLVED | Evidence, scene/event links and page ranges are read-only; no mutation API or weakened source constraints. | User-approved. |
| OPEN-04 — RESOLVED | Exactly ar/en for new requests, omitted language defaults en; no aliases/detection dependency; preserve legacy profiles. | User-approved. |
| OPEN-05 — RESOLVED | Mandatory strong If-Match with 428/400/412, no wildcard/multi-values, atomic compare/validate/write, no silent merge; coordinated rollout. Empty edits 422, true no-ops unchanged, optional dialogue/speaker null semantics in section 4. | Concurrency user-approved; precise no-op/null details finalized here. |
| OPEN-06 — RESOLVED | Add nullable approved_revision alongside revision. Backfill 1/NULL without changing legacy data or review states. Explicit normal review confirmation establishes a new binding and audit; no historical approval invented. Non-ar/en remains readable/editable/reviewable but dependency-ineligible. | Minimal legacy refinement fully specified in sections 3, 6, 9, 10, 12 and 13; awaiting approval of this revision, not an unresolved design choice. |
| OPEN-07 — RESOLVED | Version-relative validity only; newer StoryVersions do not invalidate correctly bound older scripts. No supersession state/latest-only gating. | User-approved. |

No remaining policy/schema/API ambiguity or technical specification blocker is
known. The implementation migration identifier and actual validation commands are
selected against the implementation checkout; neither changes this contract.
The explicit user approval gate remains in force.

## 17. Approval checkpoint

This resolved specification is implementation-ready as a design, NOT authorization
to implement. STOP after this documentation update. Obtain explicit user approval
of this revised specification, including the legacy refinement, before producing
and executing the bounded implementation plan. No application code, migration,
or application test run is part of this specification update.
Phase 7A remains CLOSED throughout.