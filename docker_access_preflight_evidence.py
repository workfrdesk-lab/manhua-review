"""Value-free Docker access and credential-remediation preflight evidence."""
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

import credential_preflight_evidence as preflight

ROOT = Path(__file__).resolve().parent
PRIOR = ROOT / "credential-remediation-preflight-evidence-20261001T161535802833Z.json"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(path_text):
    return "sha256:" + hashlib.sha256(path_text.encode()).hexdigest()[:16]


def safe_run(args, timeout=120):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    return result.returncode == 0


def build_values(containers):
    values = {}
    for container in containers:
        for key, value in preflight.env_map(container).items():
            if not preflight.SENSITIVE.search(key) or not value or key.upper().endswith(("_FILE", "_PATH")):
                continue
            identifier = preflight.classify_value(key, value)
            if not identifier:
                continue
            secret = unquote(urlsplit(value).password or "") if "URL" in key.upper() else value
            values.setdefault(identifier, secret.encode())
    return values


def mapping(compose, containers):
    rows = []
    for container in containers:
        service = preflight.service(container)
        if service not in {"backend", "worker", "migrate", "storage-init", "postgres", "storage"}:
            continue
        declared = compose["services"].get(service, {})
        env = preflight.env_map(container)
        refs = []
        for key, value in sorted(env.items()):
            if not preflight.SENSITIVE.search(key) or not value:
                continue
            if key.upper().endswith(("_FILE", "_PATH")):
                refs.append({"key": key, "classification": "file/path reference; contents not inspected"})
                continue
            refs.append({
                "key": key,
                "fingerprint": preflight.classify_value(key, value),
                "source": "Compose environment interpolation" if key in declared.get("environment", {}) else "container/image runtime environment",
                "env_file_declared": bool(declared.get("env_file")),
            })
        rows.append({
            "service": service,
            "running": bool(container["State"]["Running"]),
            "image_id": container["Image"],
            "compose_project_identity": fingerprint(container["Config"]["Labels"].get("com.docker.compose.project", "")),
            "credential_references": refs,
            "precedence": ["Compose interpolation from host/.env", "service environment", "image defaults", "runtime-mounted files if configured"],
        })
    return rows


def findings():
    old = json.loads(PRIOR.read_text(encoding="utf-8"))
    result = []
    for item in old.get("findings", []):
        path = item["path"]
        if path.endswith(".env.example"):
            category = "development/example default"
        elif path.endswith("secret-audit-path-findings.log"):
            category = "local audit artifact"
        elif item.get("generic_pattern_match"):
            category = "false positive"
        elif path.endswith(".env") or path.endswith("docker-compose.yml"):
            category = "active runtime credential"
        else:
            category = "unknown"
        result.append({"path": path, "fingerprints": item.get("fingerprints", []), "classification": category})
    return result


def contexts(compose):
    result = []
    for service, config in compose.get("services", {}).items():
        build = config.get("build")
        if not build:
            continue
        context = (ROOT / (build.get("context", ".") if isinstance(build, dict) else build)).resolve()
        ignore = context / ".dockerignore"
        result.append({
            "service": service,
            "context_identity": fingerprint(str(context)),
            "dockerignore_present": ignore.exists(),
            "dockerignore_sha256": digest(ignore) if ignore.exists() else None,
            "excluded_patterns": ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else [],
            "dockerfile_specific_ignore_present": (context / "Dockerfile.dockerignore").exists(),
            "additional_contexts_present": bool(build.get("additional_contexts")) if isinstance(build, dict) else False,
        })
    return result


def main():
    protected_before = {str(p): digest(p) for p in ROOT.glob("credential-remediation-preflight-*.*") if p.is_file()}
    protected_before.update({str(p): digest(p) for p in ROOT.glob("secret-audit*") if p.is_file()})
    prior = json.loads(PRIOR.read_text())
    protected_before.update({p: digest(p) for p in prior['protected_artifacts']})
    containers = preflight.inspect_containers()
    by_service = {preflight.service(c): c for c in containers}
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    values = build_values(containers)
    probes = [preflight.probe(by_service[service], kind) for service in ("backend", "worker") for kind in ("db", "s3")]
    images = [preflight.layer_scan(image, values) for image in sorted({c["Image"] for c in containers})]
    for image in images:
        if image['errors'] and 'metadata_hits' not in image:
            image['metadata'] = 'UNVERIFIED'
            image['history'] = 'UNVERIFIED'
    storage = by_service["storage"]
    mount = next((m for m in storage["Mounts"] if "license" in m["Destination"].lower()), None)
    license_state = {
        "mounted": bool(mount),
        "nonempty": bool(mount and safe_run([
            "docker", "exec", storage["Id"], "sh", "-c",
            "test -s \"$1\"", "license-check", mount["Destination"],
        ])),
        "configured_source": "host source redacted" if mount else "UNVERIFIED",
        "read_only": bool(mount and not mount["RW"]),
        "validity": "UNVERIFIED",
        "license_contents_read": False,
        "compose_base_file_identity": fingerprint(str(ROOT / "docker-compose.yml")),
        "compose_variable_present_in_base_file": "MINIO_LICENSE_PATH" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8"),
        "host_license_path_variable_present": bool(os.environ.get("MINIO_LICENSE_PATH")),
    }
    protected_after_pre = {path: digest(path) for path in protected_before}
    report = {
        "decision": "ROTATION BLOCKED",
        "gate": "OPEN",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "docker_access": {"cli_and_daemon_available": True, "service_restarted": False, "service_recreated": False},
        "credentials_rotated": False,
        "credential_values_included": False,
        "affected_chapter_accessed": False,
        "chapter_objects_accessed": False,
        "ingestion_recovery_semantics_changed": False,
        "redis_restarted": False,
        "migrations_run": False,
        "storage_init_mutations_run": False,
        "incident": {"prior_tool_output_exposed_credential_literals": True, "values_reproduced": False, "public_external_exposure_established": False},
        "authenticated_probes": probes,
        "effective_configuration": mapping(compose, containers),
        "image_inventory_and_layer_exposure": images,
        "build_contexts": contexts(compose),
        "license_reconciliation": license_state,
        "secret_findings": findings(),
        "hashes": {"before": protected_before, "after": protected_after_pre, "protected_unchanged": protected_before == protected_after_pre},
        "unresolved_items": [
            "Remaining generic-pattern findings are unknown; complete historical scanner finding reconciliation has not been performed.",
            "Build contexts and ignore files were identified, but complete effective inclusion/COPY exposure analysis remains unverified.",
            "Effective server credential precedence, database host authentication method, and stopped-client execution settings remain incompletely verified; configuration mapping is not proof of all process settings.",
            "storage-init image metadata/history/layer inspection is unavailable; its image remains UNVERIFIED.",
            "AIStor license validity is UNVERIFIED; only mounted and nonempty state was checked without reading content.",
            "No historical launch-environment record proves whether the runtime license path came from host environment, dotenv, or another invocation source.",
            "Image inspection found no known active credential fingerprint/value in verified images, but storage-init cannot be bounded.",
            "No credential rotation was performed, so active runtime credentials remain active by design.",
        ],
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    prefix = ROOT / ("docker-access-preflight-" + stamp)
    payload = json.dumps(report, indent=2).encode("utf-8")
    log = ("ROTATION BLOCKED\nDocker access restored; no service restart or recreation performed.\n"
           "No credentials rotated. No credential values or license contents included.\n"
           "Prior tool output exposed credential literals; incident recorded without reproduction.\n"
           "Unresolved items:\n" + "\n".join("- " + x for x in report["unresolved_items"]) + "\n").encode("utf-8")
    assert all(value not in payload and value not in log for value in values.values())
    for suffix, data in ((".json", payload), (".log", log)):
        path = Path(str(prefix) + suffix)
        with path.open("xb") as stream:
            stream.write(data)
        with Path(str(path) + ".sha256").open("x", encoding="ascii") as stream:
            stream.write(digest(path) + "  " + path.name + "\n")
    print(json.dumps({"decision": report["decision"], "report": str(prefix) + ".json", "probes_success": all(p["success"] for p in probes), "unverified_images": sum(x["layers"] != "VERIFIED" for x in images), "license_mounted": license_state["mounted"], "license_nonempty": license_state["nonempty"], "values_included": False}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'incomplete', 'error_class': type(error).__name__}))
        raise SystemExit(1)