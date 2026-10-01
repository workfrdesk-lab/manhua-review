"""Emit conservative closure evidence without inspecting secret or license contents."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PRIOR = ROOT / "credential-closure-preflight-20261001T163841969032Z.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_license_path(path: str) -> bool:
    value = path.lower().replace("\\", "/")
    return "license" in value or "minio.license" in value


def main() -> None:
    prior = json.loads(PRIOR.read_text(encoding="utf-8"))
    prior_hashes = prior.get("protected_hashes_before", {})
    protected = {}
    before = {}
    excluded_license_paths = []
    for name, expected in prior_hashes.items():
        path = Path(name)
        if is_license_path(name):
            excluded_license_paths.append(name)
            continue
        if not path.is_file():
            protected[name] = {"status": "UNVERIFIED", "reason": "artifact missing"}
            continue
        actual = sha256(path)
        before[name] = actual
        protected[name] = {
            "status": "VERIFIED" if actual == expected else "UNVERIFIED",
            "hash_match": actual == expected,
        }

    copy_status = [
        {
            "service": item["service"],
            "context_path": item["context"],
            "dockerfile_hash_status": "UNVERIFIED",
            "dockerignore_hash_status": "UNVERIFIED",
            "exclusion_rules_status": "UNVERIFIED",
            "source_destination_mapping_status": "UNVERIFIED",
            "reason": "Prior inventory does not prove source/destination mapping or effective exclusions; no fresh source inspection performed.",
            "content_level_secret_classification": "UNVERIFIED",
            "effective_inclusion_status": "UNVERIFIED",
            "recursive_context_copy_possible": any(
                instruction.get("recursive_whole_context") for instruction in item.get("instructions", [])
            ),
        }
        for item in prior.get("copy_analysis", [])
    ]

    findings = [
        {
            "path": item["path"],
            "path_type_context_status": "UNVERIFIED",
            "classification": "UNVERIFIED",
            "matched_literal_emitted": False,
            "reason": "Prior contextual evidence insufficient; no new content inspection performed.",
        }
        for item in prior.get("remaining_findings", [])
    ]

    report = {
        "decision": "ROTATION BLOCKED",
        "gate": "CREDENTIAL REMEDIATION = OPEN",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "procedure": {
            "raw_secret_inspection": "NOT_APPLICABLE",
            "broad_searches": "NOT_APPLICABLE",
            "docker_environment_dump": "NOT_APPLICABLE",
            "image_filesystem_or_layer_extraction": "NOT_APPLICABLE",
            "new_connectivity_probes": "NOT_APPLICABLE",
            "license_contents_read_hashed_copied_decoded_or_modified": "NOT_APPLICABLE",
        },
        "storage_init_image": {
            "referenced_image_available": False,
            "availability_status": "UNVERIFIED",
            "evidence_basis": "Prior metadata checks reported ImageNotFound; not repeated in this pass.",
            "historical_layer_provenance": "UNVERIFIED",
            "metadata_history_layers": "UNVERIFIED",
            "filesystem_recovery_attempted": False,
            "substitution_attempted": False,
        },
        "build_copy_analysis": copy_status,
        "generic_findings": findings,
        "credential_precedence": {
            "running_services": "UNVERIFIED",
            "stopped_services": "UNVERIFIED",
            "environment_variable_sources": "UNVERIFIED",
            "file_reference_sources": "UNVERIFIED",
            "postgresql_server_authentication_configuration": "UNVERIFIED",
            "successful_prior_probes_repeated": False,
        },
        "aistor_license": {
            "mounted": prior.get("license", {}).get("mounted"),
            "nonempty": prior.get("license", {}).get("nonempty"),
            "read_only": prior.get("license", {}).get("read_only"),
            "current_metadata_status": "UNVERIFIED",
            "evidence_basis": "Historical boolean observations only; not rechecked this pass.",
            "source_identity_path_relationship": "UNVERIFIED",
            "validity": "UNVERIFIED",
            "contents_accessed": False,
            "validity_blocks_rotation": False,
        },
        "security_incident": {
            "raw_credential_literals_exposed_during_audit": True,
            "values_reproduced_in_this_report": False,
            "public_or_external_exposure_established": False,
        },
        "safety_invariants": {
            "credentials_rotated": False,
            "redis_restarted": False,
            "migrations_run": False,
            "storage_init_mutated": False,
            "affected_chapter_or_object_access": False,
            "ingestion_or_recovery_code_changed": False,
            "service_recreated_for_audit": False,
        },
        "protected_evidence": {
            "prior_artifact_count": len(prior_hashes),
            "license_paths_excluded_from_hash_verification": len(excluded_license_paths),
            "non_license_artifact_statuses": protected,
            "all_non_license_hashes_unchanged": all(
                item.get("status") == "VERIFIED" for item in protected.values()
            ),
        },
        "remaining_blockers": [
            "Storage-init historical layers remain UNVERIFIED because the exact referenced image is absent.",
            "Effective COPY/ADD inclusion and content-level secret classification remain UNVERIFIED.",
            "Generic scanner findings remain individually UNVERIFIED without matched-value inspection.",
            "Complete running/stopped environment, *_FILE, and PostgreSQL authentication precedence remains UNVERIFIED.",
        ],
        "no_values_or_license_contents_included": True,
    }
    unchanged_during_pass = all(Path(name).is_file() and sha256(Path(name)) == value
                                for name, value in before.items())
    report["protected_evidence"]["unchanged_during_this_pass"] = unchanged_during_pass
    report["protected_evidence"]["status"] = (
        "VERIFIED" if report["protected_evidence"]["all_non_license_hashes_unchanged"]
        and unchanged_during_pass else "UNVERIFIED"
    )
    if report["protected_evidence"]["status"] != "VERIFIED":
        report["remaining_blockers"].append(
            "Protected evidence is missing or differs from the historical baseline; cause not established."
        )
    report["remaining_blockers"].append(
        "Expected license source/path relationship remains UNVERIFIED; validity itself is non-blocking."
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    prefix = ROOT / f"security-safe-closure-preflight-{stamp}"
    json_path = prefix.with_suffix(".json")
    log_path = prefix.with_suffix(".log")
    with json_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, indent=2))
    with log_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(
        "ROTATION BLOCKED\n"
        "CREDENTIAL REMEDIATION = OPEN\n"
        "No credential values or license contents are included.\n"
        "Raw credential literals were exposed during the audit pass; public/external exposure was not established.\n"
        + "\n".join(f"BLOCKER: {item}" for item in report["remaining_blockers"])
        + "\n",
    )
    for path in (json_path, log_path):
        sidecar = Path(str(path) + ".sha256")
        with sidecar.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(f"{sha256(path)}  {path.name}\n")
        assert sidecar.read_text(encoding="ascii").split()[0] == sha256(path)
    print(json.dumps({"decision": report["decision"], "report": str(json_path), "sidecars_verified": True,
                      "historical_baseline_matches": report["protected_evidence"]["all_non_license_hashes_unchanged"],
                      "unchanged_during_this_pass": unchanged_during_pass,
                      "baseline_unverified_count": sum(x["status"] != "VERIFIED" for x in protected.values()),
                      "license_contents_accessed": False}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"status":"UNVERIFIED","reason":"Report generation failed; error details suppressed"}')
        raise SystemExit(1)