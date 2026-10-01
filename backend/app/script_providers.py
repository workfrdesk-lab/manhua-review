"""Script capability on the existing provider boundary.

The deterministic implementation is intentionally conservative: it emits no script
unless the StoryVersion contains enough grounded events and source evidence.
"""

import json
from hashlib import sha256
from typing import Protocol

from app.script_schemas import (
    ScriptEvidenceData,
    ScriptProfile,
    ScriptSegmentData,
    ScriptStructuredOutput,
)
from app.story_schemas import StoryStructuredOutput


class ScriptProvider(Protocol):
    name: str
    model: str

    def generate_script(
        self, story: StoryStructuredOutput, profile: ScriptProfile
    ) -> ScriptStructuredOutput: ...


class DeterministicScriptProvider:
    name = "deterministic"
    model = "fixture-script-v1"

    def generate_script(self, story, profile):
        segments = []
        sequence = 1
        for scene in story.scenes:
            for event in scene.events:
                if not event.evidence:
                    continue
                evidence = event.evidence[0]
                segments.append(
                    ScriptSegmentData(
                        temp_id=f"script-{sequence}",
                        sequence=sequence,
                        narration_text=event.description,
                        scene_ref=scene.temp_id,
                        event_refs=[event.temp_id],
                        start_page=evidence.page_number or scene.start_page,
                        end_page=evidence.page_number or scene.end_page,
                        estimated_duration=max(1.0, len(event.description) / 14),
                        confidence=min(event.confidence, scene.confidence),
                        evidence=[
                            ScriptEvidenceData(
                                **evidence.model_dump(exclude={"reason"}),
                                scene_ref=scene.temp_id,
                                event_ref=event.temp_id,
                                reason="Grounded in the StoryVersion event evidence",
                            )
                        ],
                    )
                )
                sequence += 1
        if not segments:
            raise ValueError("StoryVersion has no grounded events for script generation")
        return ScriptStructuredOutput(
            title=f"{profile.target_style.title()} script",
            hook=segments[0].narration_text,
            segments=segments,
        )


def configured_script_provider() -> ScriptProvider:
    from app.story_providers import ProviderNotConfigured, configured_provider

    provider = configured_provider()
    if not callable(getattr(provider, "generate_script", None)):
        raise ProviderNotConfigured("Configured provider does not support script generation")
    return provider


def profile_fingerprint(profile: ScriptProfile) -> str:
    return sha256(json.dumps(profile.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
