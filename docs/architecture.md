# Architecture decision record — Phase 1

## Reference audit

Reference: https://github.com/allwin-antony/ManhwaForge
Reviewed revision: `9b225998378d793aa36c06ca21d95e4aa61f079c`.

Reviewed the recursive tree, README, CLI orchestrator, all six processing modules, requirements, YAML configurations, tests, GPU check, overview and sample output. This was source inspection, not execution.

The reference is a local Python CLI: PDF → OpenCV panel slicing → PaddleOCR → per-panel Ollama narration → Edge TTS → FFmpeg. State is stored in JSON and assets on disk. There is no web backend, database, authentication, queue, billing or editor.

### Licensing decision

No LICENSE file was present and GitHub reported `license: null`. A public repository and an educational disclaimer are not a reuse license. No source, prompts, configurations or sample content from ManhwaForge have been copied into this application. It is acknowledged as a conceptual reference only. Any future reuse requires an explicit suitable license or permission.

PyMuPDF uses AGPL/commercial licensing; do not add it by default. PDFium/pypdfium2 is a candidate pending version-specific license review. Audit FFmpeg builds, codecs, fonts, model weights, voices, music and SFX independently. MinIO is a separate AGPL-licensed service; deployment/distribution obligations require review. This is an engineering decision, not legal advice.

### Source-level findings

- Orchestrator calls `generate_sync` and `get_audio_duration`, absent in the TTS class.
- `imageio_ffmpeg` is imported but not declared in requirements.
- Camera movement flags do not produce movement in the filter graph.
- Renderer hardcodes libx264 rather than selecting hardware encoding.
- Vision measures color/edges rather than understanding characters or events.
- Narration uses OCR text, with hardcoded fictional fallback events.
- TTS failure produces silence and reports success.
- OCR failures are indistinguishable from pages without text.
- Slicing deletes source page images.
- Failed clips may be skipped; music mixing return status is not checked.
- Three limited tests do not validate the end-to-end pipeline.

## Target design (planned, not implemented)

Next.js/TypeScript/Tailwind → FastAPI → PostgreSQL and S3-compatible storage.
Celery/Redis drives separate ingestion, AI, audio and render queues. FFmpeg runs only in workers. Use a modular monolith initially rather than distributed microservices with duplicated business logic.

SQLAlchemy/Alembic will own migrations. Required user/project/chapter/page/panel/character/scene/script/voice/asset/job/video/key/settings entities will be supplemented by sessions, OAuth identities, script versions, story events, relationships, credit ledger/reservations, attempts and audit events.

Provider interfaces: LLM, TTS, STT, image, video and storage. Expose actual provider capabilities, not invented pitch/emotion options. Cloudinary needs a distinct adapter. Edge TTS is network-backed, optional and not the sole production voice provider. Select local models only after language, hardware and licensing validation.

The pipeline stops at script review. User approval is required before TTS. Every output records input version and provider settings. Edits invalidate dependent outputs. Keep originals; retain evidence panel IDs for story events. Low confidence is visible, not replaced with invented facts.

Retries use idempotency keys and stage checkpoints. Credits use transactional reserve/settle/release, never double-charge on retries. A failed required audio/render segment cannot be silently omitted. Progress derives from completed units and FFmpeg timestamps. Validate final MP4 with FFprobe before reporting completion.

Timeline supports independent visual/audio/subtitle/music/SFX tracks. ASS/libass and audio alignment need Arabic shaping and timing tests. Actual rendered previews are distinguishable from editor approximations. Use ducking, fades and licensed assets. Hardware encoders require a runtime smoke test and CPU fallback.

Security planned before uploads: ownership checks, Argon2id, secure opaque sessions, CSRF, OAuth state/PKCE, expiring one-use reset tokens, API key encryption with external key management, private object URLs, ZIP traversal/bomb defenses, content sniffing, quotas, isolated work directories and worker limits. Uploaded/OCR text is untrusted data, not model instructions. No unauthorized chapter downloader.

## Phase gates

1. Audit/architecture: documented.
2. Structure: unit tests/build pass; real Compose integration still required.
3. Auth/database: migrations, login/reset/OAuth, tenant isolation.
4. Upload: supported files, limits, extraction and ordering.
5. OCR/panels: persisted results, manual correction, partial failures.
6. Story: evidence-linked events and real vision integration.
7. Script: Arabic/English, editing, approval and version invalidation.
8. TTS: actual audio, character mapping, provider errors/retries.
9. Timeline: persistent edits and asset consistency.
10. Rendering: real MP4, actual progress, FFprobe validation.
11. Subtitles/music/SFX: timing, Arabic, ducking, rights metadata.
12. Credits: concurrency, reservations and retry accounting.
13. Admin: authorization, real records and auditing.
14. Production: hardened deployment, secrets, backup/restore, monitoring and E2E.

Update README and execute acceptance checks at each gate; do not claim skipped integrations passed.