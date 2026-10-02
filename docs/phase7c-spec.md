# Phase 7C — Approved Script Handoff

**Final proposed specification for:**
`D:\projects\manhua review\docs\phase7c-spec.md`

**Status: specification decisions locked; implementation NOT authorized.**

This document defines the final proposed Phase 7C v1 contract. Closing specification decisions does not authorize implementation.

The document is displayed only. Creating this file, modifying project files, adding or running implementation tests, performing migrations, changing frontend behavior, committing, and pushing are not authorized by this review.

---

## 1. Architectural decision

Phase 7C is an independent phase named:

> **Approved Script Handoff**

Its purpose is to expose a coherent, currently eligible approved-script representation to downstream consumers without implementing those consumers.

The official endpoint is:

```http
GET /api/v1/chapters/{chapter_id}/scripts/{script_version_id}/handoff
```

The canonical internal backend service is the sole source of handoff eligibility, dependency identity, DTO construction, and serialization.

Future Phase 8 code running inside the same backend must call this service directly, not through HTTP loopback.

Phase 7A and Phase 7B behavior and contracts remain unchanged.

---

## 2. Scope

Phase 7C v1 includes:

- A canonical internal handoff service.
- One authenticated, chapter-owned, read-only HTTP endpoint.
- Exact five-field dependency identity.
- Reuse of existing revision-bound approval and grounding semantics.
- One explicit versioned success representation.
- Minimal evidence projection.
- Existing segment `estimated_duration`.
- Deterministic machine-readable ineligibility responses.
- Coherent PostgreSQL snapshot requirements.
- Mandatory `Cache-Control: no-store`.
- Future-consumer pre-start and pre-publication obligations.

The response is a transient representation of database state. It is not a persisted immutable export, a durable approval certificate, or a downstream artifact.

### 2.1 Non-scope

Phase 7C v1 does not include:

- TTS, audio, subtitles, timelines, rendering, or video.
- Voice selection, speaker inference, or character-to-voice mapping.
- Frontend changes, handoff buttons, JSON viewers, exports, or downloads.
- Migrations, tables, columns, indexes, or enums.
- Workers, queues, jobs, retries, or provider execution.
- Storage objects, media artifacts, or persisted handoff packages.
- Artifact retention, cleanup, or invalidation jobs.
- StoryAudit mutation events for handoff reads.
- ETag or conditional GET optimization.
- External-consumer authorization mechanisms.
- Webhooks, new roles, billing, or workflow orchestration.
- Changes to Phase 7A or Phase 7B semantics.

Existing authentication and ownership infrastructure is reused. No new external integration credentials are introduced.

---

## 3. Canonical service boundary

### 3.1 Responsibilities

The canonical service must:

1. Accept an authenticated authorization context and explicit chapter/script identifiers.
2. Authorize access to the requested chapter.
3. Verify that the script belongs to that chapter.
4. Load the exact bound StoryVersion.
5. Load the current source records needed by existing grounding validation.
6. Evaluate existing approval, revision, language, and source-validity rules.
7. Construct the exact dependency identity.
8. Construct the approved-script handoff DTO from the same coherent database view.
9. Apply the v1 projection and serialization rules.

It returns either:

- the canonical eligible handoff DTO; or
- a typed failure identifying missing/unauthorized access or current ineligibility.

Unexpected infrastructure errors must not be relabeled as normal dependency ineligibility.

### 3.2 Canonical means shared, not duplicated

Phase 7C must reuse the existing Phase 7B eligibility semantics and validator. It must not introduce a second, subtly different approval policy.

The HTTP adapter and future Phase 8 consumers must not independently:

- calculate dependency identity;
- infer eligibility from script status;
- serialize ORM rows into handoff content;
- rebuild evidence projections;
- substitute the latest StoryVersion;
- trust client-supplied eligibility.

Any implementation-time internal consolidation must preserve existing Phase 7A/7B observable behavior, including reason semantics, authorization, revision handling, and existing ETag behavior on existing endpoints.

### 3.3 Content source and serialization

The service must validate the bound script content using the existing structured script contract and existing grounding validator.

Projection must come from that validated content and the source records used during validation. Where a page number must be resolved from a page identifier, resolution must use that same snapshot.

It must not combine validated JSON from one revision with independently cached normalized rows from another revision.

The DTO must be fully materialized before leaving the coherent read boundary. Serialization must not trigger later ORM lazy loads or source re-fetches.

### 3.4 Consumers

- **HTTP:** a transport adapter over the service.
- **Future Phase 8 in the same backend:** a direct service caller.
- **External consumers:** may use the HTTP boundary under existing applicable authorization; a separate external authorization mechanism is outside Phase 7C.

A background consumer must carry an authorized ownership scope. “Internal caller” is not a bypass for ownership checks.

---

## 4. Exact dependency identity

The identity consists of exactly:

| Field | Type | Meaning |
|---|---|---|
| `story_version_id` | UUID string | Exact bound StoryVersion |
| `script_version_id` | UUID string | Exact ScriptVersion |
| `revision` | Positive integer | Current script revision |
| `language` | String | Stored profile language under existing Phase 7B semantics |
| `profile_fingerprint` | String | Existing stored generation profile fingerprint |

The service must preserve the existing fingerprint. It must not recompute it using current provider configuration.

The following are not identity fields:

- `schema`;
- `eligible`;
- `reasons`;
- approval status;
- timestamps;
- HTTP headers;
- authorization context.

Chapter ownership is a separate required authorization condition, not a sixth dependency field.

For downstream validity, all five identity fields must match the consumer’s persisted input identity.

---

## 5. Eligibility rules

A successful handoff requires:

1. An authenticated, authorized caller.
2. Ownership of the requested chapter.
3. A script belonging to the requested chapter.
4. The exact script-to-StoryVersion binding.
5. `ScriptVersion.status == confirmed`.
6. `ScriptVersion.approved_revision == ScriptVersion.revision`.
7. Supported language: `ar` or `en`.
8. Successful current source and grounding validation against the exact bound StoryVersion.

Current validation includes the existing applicable checks for source existence, page/panel/OCR relationships, event references, evidence, quotes, dialogue, ordering, and confidence.

Phase 7C must not add:

- a requirement that the bound StoryVersion be the latest;
- a `story_superseded` reason;
- a new confidence threshold;
- a new speaker-attribution capability;
- an independent segment-approval eligibility rule beyond existing Phase 7B behavior;
- semantic-entailment guarantees.

A missing bound StoryVersion encountered while validating an otherwise accessible script is a source-validation failure. A missing or unauthorized requested chapter/script is a `404`, not an eligibility response.

### 5.1 Existing deterministic reasons

| Reason | Condition |
|---|---|
| `review_required` | Script status is `needs_review` |
| `rejected` | Script status is `rejected` |
| `approval_unbound` | Script is confirmed but `approved_revision != revision`, including null approval |
| `unsupported_language` | Language is outside the Phase 7B supported set |
| `source_invalid` | Existing source/grounding validation fails |

Return every applicable reason, without duplicates, in lexical ascending order.

Authorization must succeed before returning eligibility information.

### 5.2 Meaning of `eligible: true`

It means the checks succeeded within the request’s coherent database snapshot.

It is not:

- permanent authorization;
- a lease or lock;
- permission to publish later;
- proof of future source availability;
- a substitute for current validation.

### 5.3 Required downstream rechecks

#### At GET

The service evaluates authorization, exact binding, current approval, revision, language, and source/grounding validity.

#### Before downstream work starts

The future consumer must:

- recheck ownership/scope;
- obtain current eligibility through the canonical service;
- compare all five identity fields with its intended/persisted inputs;
- verify its own provider/settings/input identity;
- refuse to silently upgrade a request to another script revision.

#### Immediately before publication

The future consumer must repeat those checks.

Its publication design must address the race between validation and publication; a detached successful GET is insufficient. The atomic publication/fencing mechanism belongs to the producing phase, not Phase 7C.

Historical output may remain historical, but a mismatch must prevent it from being represented as current valid output. Phase 7C neither creates nor deletes such output.

---

## 6. Exact success response schema

### 6.1 Transport

```http
200 OK
Content-Type: application/json
Cache-Control: no-store
```

### 6.2 Schema definition

The following tables define the complete v1 response shape. All listed keys are required. Nullable fields must be emitted as `null` when absent, not omitted.

No additional fields belong to the v1 representation.

#### Root object

| Key | Type / constraint |
|---|---|
| `schema` | Literal `"approved-script-handoff-v1"` |
| `dependency` | Dependency object below |
| `script` | Script object below |

#### Dependency object

| Key | Type / constraint |
|---|---|
| `story_version_id` | UUID string |
| `script_version_id` | UUID string |
| `revision` | Integer ≥ 1 |
| `language` | `"ar"` or `"en"` |
| `profile_fingerprint` | Existing stored SHA-256 fingerprint string |
| `eligible` | Literal `true` |
| `reasons` | Empty array `[]` |

#### Script object

| Key | Type / constraint |
|---|---|
| `title` | String, 1–300 characters |
| `hook` | String, 0–5000 characters |
| `intro` | String, 0–5000 characters |
| `outro` | String, 0–5000 characters |
| `segments` | Array of 1–200 segment objects |

Front matter preserves existing script metadata. It is not an instruction to narrate duplicate segment text twice.

#### Segment object

| Key | Type / constraint |
|---|---|
| `sequence` | Integer ≥ 1; contiguous order starting at 1 |
| `segment_type` | `narration`, `dialogue`, `transition`, `intro`, or `outro` |
| `narration_text` | String, 1–5000 characters |
| `dialogue_text` | Null or string, 1–1000 characters |
| `source_dialogue` | Boolean |
| `speaker_ref` | Null or string, 1–100 characters |
| `scene_ref` | String, 1–100 characters |
| `event_refs` | Array of 1–100 unique reference strings |
| `start_page` | Integer ≥ 1 |
| `end_page` | Integer ≥ `start_page` |
| `estimated_duration` | Finite number of seconds, > 0 and ≤ 3600 |
| `confidence` | Finite number between 0 and 1 inclusive |
| `status` | Existing validated segment review state: `needs_review`, `confirmed`, or `rejected` |
| `evidence` | Array of 1–100 evidence objects |

Segment status is preserved, not synthesized. Successful handoff eligibility is governed by the existing script-level approval and validation contract; Phase 7C does not add a separate segment-status policy.

`estimated_duration` is the existing script estimate. It is not measured audio duration, a scheduling guarantee, or a TTS setting.

Existing dialogue consistency and speaker validation rules remain authoritative. Including `speaker_ref` does not authorize new speaker attribution.

#### Evidence object

| Key | Type / constraint |
|---|---|
| `scene_ref` | String, 1–100 characters |
| `event_ref` | String, 1–100 characters |
| `page_number` | Resolved integer ≥ 1 |
| `panel_id` | Null or UUID string |
| `ocr_result_id` | Null or UUID string |
| `quote` | Null or string, maximum 1000 characters |

The projection preserves the evidence sequence in the validated script representation. It must not use incidental database row order.

### 6.3 Representative success response

```json
{
  "schema": "approved-script-handoff-v1",
  "dependency": {
    "story_version_id": "11111111-1111-4111-8111-111111111111",
    "script_version_id": "22222222-2222-4222-8222-222222222222",
    "revision": 4,
    "language": "ar",
    "profile_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "eligible": true,
    "reasons": []
  },
  "script": {
    "title": "بداية الرحلة",
    "hook": "",
    "intro": "",
    "outro": "",
    "segments": [
      {
        "sequence": 1,
        "segment_type": "narration",
        "narration_text": "يغادر البطل القرية.",
        "dialogue_text": null,
        "source_dialogue": false,
        "speaker_ref": null,
        "scene_ref": "scene-1",
        "event_refs": ["event-1"],
        "start_page": 1,
        "end_page": 1,
        "estimated_duration": 4.5,
        "confidence": 0.91,
        "status": "confirmed",
        "evidence": [
          {
            "scene_ref": "scene-1",
            "event_ref": "event-1",
            "page_number": 1,
            "panel_id": null,
            "ocr_result_id": null,
            "quote": null
          }
        ]
      }
    ]
  }
}
```

This example illustrates the shape; it does not represent a repository record.

---

## 7. Minimal evidence projection

The exact public evidence projection is:

```text
scene_ref
event_ref
page_number
panel_id
ocr_result_id
quote
```

The following are not emitted in v1:

- `reason`;
- `page_id`;
- `evidence_id`;
- `character_ref`;
- raw OCR text;
- raw StoryVersion data;
- private URLs or storage keys;
- raw ORM fields.

These omissions do not remove internal validation requirements. The backend must still use all required internal fields, including page IDs, evidence reasons where required by the existing schema, and character references during validation.

Grounding traceability is retained through:

- exact StoryVersion identity;
- segment scene/event references;
- evidence scene/event references;
- resolved chapter-local page number;
- panel/OCR identifiers where applicable;
- source quote where present;
- segment page ranges.

The projection is not a standalone offline evidence archive. It does not guarantee continued source availability.

`source_dialogue` and `dialogue_text` remain segment fields. Raw OCR source text is not duplicated into the handoff.

Confidence remains existing metadata, not a semantic correctness guarantee or a new approval threshold.

---

## 8. Exact ineligible response

### 8.1 Transport

```http
409 Conflict
Content-Type: application/json
Cache-Control: no-store
```

### 8.2 Exact envelope

```json
{
  "success": false,
  "error": {
    "code": "DEPENDENCY_NOT_ELIGIBLE",
    "message": "Script is not eligible for downstream use",
    "reasons": [
      "approval_unbound",
      "source_invalid"
    ]
  }
}
```

Rules:

- `success` is always `false`.
- `code` is always `DEPENDENCY_NOT_ELIGIBLE`.
- `message` is the fixed string shown above.
- `reasons` is a nonempty array drawn only from the five reasons in Section 5.1.
- Reasons are unique and lexically sorted.
- No script content, dependency object, resource identifiers, or validation internals are included.
- No extra fields are included.

The example shows two applicable reasons; actual responses contain exactly the applicable reasons.

This structured handoff error is an additive endpoint-specific contract. The existing generic error handler currently does not provide this reasons array automatically. Future implementation must preserve this envelope without changing Phase 7A/7B error behavior or stringifying the structured payload.

Unauthorized callers must receive the existing non-disclosing access response, not this `409` envelope.

---

## 9. HTTP semantics

| Status | Required meaning |
|---|---|
| `200` | Authorized, eligible handoff |
| `401` | Unauthenticated request under existing authentication policy |
| `404` | Missing or unauthorized chapter/script, including script/chapter mismatch |
| `409` | Authorized script is currently ineligible |
| `422` | Request validation failure, such as malformed UUID |
| `500` | Unexpected failure through sanitized error handling; no partial handoff |

Existing shared security middleware remains authoritative. Phase 7C introduces no relaxation of authentication, origin, or applicable CSRF protections.

### 9.1 Cache control

`Cache-Control: no-store` is mandatory for success and handoff endpoint error responses.

A failed response must not lose this header merely because an exception handler constructs the response.

### 9.2 ETag

ETag is outside Phase 7C v1.

The endpoint:

- does not emit ETag;
- does not require `If-Match`;
- does not implement `304 Not Modified`;
- does not use conditional request headers to bypass eligibility.

Existing Phase 7B script-edit ETag behavior is unchanged.

### 9.3 Information disclosure

Missing, cross-owner, and mismatched requested resources must not expose:

- script identity;
- eligibility reasons;
- source state;
- approval state;
- another owner’s resource existence.

---

## 10. PostgreSQL concurrency and coherent snapshots

### 10.1 Required guarantee

A response must describe one coherent committed database snapshot.

The same snapshot must cover:

- ownership and chapter/script relationships;
- script revision and approval binding;
- script content;
- bound StoryVersion;
- source pages, panels, and OCR;
- eligibility evaluation;
- evidence projection.

A mixed-revision or mixed-source response is forbidden.

### 10.2 Snapshot boundary

For PostgreSQL, the approved design is a read-only, repeatable-read transaction covering all handoff database reads.

The snapshot boundary must be established before loading any records used in the decision. Previously loaded ORM objects must not be reused as though they belonged to that snapshot.

An ordinary multi-query `READ COMMITTED` transaction does not, by itself, meet this requirement.

No no-change `UPDATE`, writer-lock helper, or approval mutation may be used to simulate a read lock.

The DTO must be fully materialized inside the snapshot. Later serialization must not read current state again.

### 10.3 Allowed concurrent outcomes

If an edit, rejection, or source change commits after the snapshot has been established:

- a coherent response for the earlier eligible snapshot is allowed;
- it may already be stale when received;
- downstream rechecks remain mandatory.

If the change committed before the snapshot:

- the service must observe the new state;
- it must return the new eligible representation or the appropriate failure.

This is snapshot consistency, not a promise that data remains current until response delivery.

### 10.4 Required PostgreSQL verification scenarios

Future authorized verification must use real PostgreSQL and controlled independent transactions, not SQLite alone or timing-only sleeps.

Required scenarios:

1. Metadata edit commits between script load and projection.
2. Segment text/order changes commit between content reads.
3. Approval/rejection changes commit during eligibility evaluation.
4. Source OCR content changes during validation without changing script revision.
5. Page/panel/OCR deletion or invalidation races with a handoff read, respecting existing deletion constraints.
6. Several readers establish snapshots on opposite sides of a mutation commit.
7. Serialization occurs after the database transaction closes and performs no lazy reads.
8. A session containing previously loaded entities cannot produce stale/mixed snapshot data.
9. Unauthorized access never produces an eligibility response during concurrent changes.
10. Successful and unsuccessful reads produce no persistent writes.

Assertions must cover both identity and complete projected content, not only HTTP status.

Downstream start/publication race tests belong to Phase 8 when those operations exist.

---

## 11. No-mutation guarantees

Every handoff read, whether successful or unsuccessful, must leave persistent application state unchanged.

It must not:

- increment revision;
- bind, clear, or repair approval;
- update timestamps through a no-change write;
- modify script or StoryVersion data;
- normalize persisted review states;
- repair or rebind evidence;
- create audit mutation records;
- create jobs or retry records;
- enqueue work;
- call a provider;
- write storage;
- create an export or artifact.

Validation that needs temporary copies must operate on copies, not mutate persisted objects.

Repeated requests create no durable side effects. Their results may differ when committed source or approval state changes.

No idempotency key is required for this GET.

---

## 12. Compatibility and versioning

The schema identifier is:

```text
approved-script-handoff-v1
```

This identifies the representation, not the dependency or approval.

Phase 7C must preserve:

- existing Phase 7A generation contracts;
- Phase 7B approval and revision semantics;
- existing dependency reasons and ordering;
- existing script-read behavior for legacy records;
- existing mutation ETag behavior;
- existing source deletion rules;
- existing authorization and sanitized error handling.

Unsupported legacy languages remain readable through existing APIs but are ineligible for handoff.

A breaking change to the handoff representation requires an explicitly reviewed schema version change. It must not silently repurpose v1 fields for TTS settings, media status, or new approval semantics.

---

## 13. Acceptance criteria

1. The endpoint path matches this specification exactly.
2. Only an authenticated chapter owner can obtain the handoff.
3. Script/chapter mismatch and cross-owner access return non-disclosing `404` responses.
4. The service preserves exact StoryVersion binding.
5. The identity contains exactly the existing five identity fields.
6. Success requires current Phase 7B eligibility in a coherent snapshot.
7. Ineligible scripts return the exact `409` envelope.
8. Every applicable reason is returned once, in lexical order.
9. Success uses the exact v1 field set and nullability rules.
10. `estimated_duration` is included without recalculation.
11. Evidence excludes `reason`, `page_id`, `evidence_id`, and other non-v1 fields.
12. Omitting evidence fields from transport does not weaken internal validation.
13. Segment/evidence ordering is deterministic.
14. Existing dialogue and speaker-attribution constraints remain enforced.
15. No new segment-approval or latest-StoryVersion eligibility rule is introduced.
16. All endpoint responses carry `Cache-Control: no-store`.
17. No ETag or `304` behavior is introduced.
18. API and internal consumption use the same canonical service and serialization.
19. PostgreSQL reads cannot produce mixed-revision or mixed-source responses.
20. Serialization performs no post-snapshot database reads.
21. Successful and failed requests cause no persistent mutations.
22. No frontend, migration, worker, queue, provider, storage object, media artifact, or audit mutation is introduced.
23. Existing Phase 7A/7B externally observable behavior remains unchanged.

---

## 14. Release gates

These gates describe future authorized implementation verification. They do not authorize running tests or implementation work during this specification review.

### 14.1 Authorization gate

Implementation must not begin until explicit implementation approval is given.

Specification lock alone is insufficient.

### 14.2 Contract and service gates

- Verify the exact success field set, nullability, ordering, and duration bounds.
- Verify the exact `409` envelope and all reason combinations applicable under existing semantics.
- Verify canonical internal DTO and HTTP representation parity.
- Verify no independent eligibility or identity logic exists in the HTTP adapter.
- Verify unchanged stored fingerprint semantics.
- Verify reduced evidence projection does not bypass validation.

### 14.3 Security and HTTP gates

- Authentication and ownership matrix passes.
- Cross-owner resource existence is not disclosed.
- `no-store` is present on success and error paths.
- Malformed requests preserve existing sanitized validation behavior.
- No ETag or conditional caching behavior is present.
- Unexpected exceptions do not leak paths, SQL, credentials, or source internals.

### 14.4 Database and concurrency gates

- All required scenarios run on disposable real PostgreSQL.
- Isolation level and snapshot start are verified.
- Independent transaction synchronization is deterministic.
- Both content coherence and eligibility coherence are asserted.
- No application-table writes, audit rows, jobs, or storage operations occur.

SQLite-only results are not sufficient for the PostgreSQL release gate.

### 14.5 Regression and repository gates

- Relevant existing Phase 7A/7B script, revision, approval, grounding, authorization, and source-integrity tests pass.
- Relevant existing PostgreSQL tests pass.
- Repository-established backend static checks pass.
- Working changes contain no migration or frontend modification.
- No new worker, provider, queue, storage object, or artifact lifecycle is introduced.
- Diff validation passes.
- Skipped or unavailable gates are explicitly reported; they must not be represented as passing.

---

## 15. Decision closure and implementation authority

All previously open Phase 7C specification decisions are closed:

- Independent phase: **Approved Script Handoff**.
- Official name and endpoint fixed.
- `reason`, `page_id`, and `evidence_id` excluded from v1 evidence.
- `estimated_duration` included.
- Exact deterministic `409` contract fixed.
- `Cache-Control: no-store` mandatory.
- ETag excluded.
- No frontend changes.
- No migration, worker, queue, provider, storage object, media artifact, or audit mutation.
- Canonical internal service is the sole handoff authority.
- Future in-backend Phase 8 uses direct service calls, not HTTP loopback.
- External-consumer authorization mechanisms are out of scope.
- Phase 7A/7B contracts remain unchanged.

There are no remaining OPEN specification decisions in this proposal. Out-of-scope Phase 8 concerns are not unresolved Phase 7C decisions.

**Implementation remains explicitly unauthorized until separate, explicit approval is provided.**

---

**تأكيد إغلاق القرارات:** أُغلقت جميع القرارات المفتوحة سابقًا في هذه المسودة النهائية.

**تأكيد عدم التغيير:** لم يتم تعديل أو إنشاء أي ملف، بما في ذلك `D:\projects\manhua review\docs\phase7c-spec.md`. لم يُنفَّذ implementation أو migration أو tests أو frontend changes أو commit أو push.