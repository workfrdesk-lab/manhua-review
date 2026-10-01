"""Capture one isolated diagnostic run, with durable evidence before teardown."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
PREFIX = ROOT / ("diagnostic-ingestion-409-valid-" + STAMP)
PROJECT = "diag409-" + STAMP.lower()
CMD = ["docker", "compose", "--ansi", "never", "-p", PROJECT,
       "-f", str(ROOT / "diagnostic-409.compose.yml")]
ENV = {**os.environ, "COMPOSE_MENU": "false", "COMPOSE_PROGRESS": "plain"}
PROTECTED = [p for p in ROOT.iterdir() if p.is_file() and (
    p.suffix in (".json", ".log") or p.name == "final_acceptance_runner.py")]
PROTECTED += list((ROOT / "backend").rglob("*.py"))


def hashes():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}


def run(args, timeout=180):
    result = subprocess.run(CMD + args, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=ENV, timeout=timeout)
    return result.returncode, result.stdout + result.stderr


def main():
    before = hashes()
    evidence = {"classification": "unresolved", "ingestion_recovery": "OPEN",
                "project": PROJECT, "reproduced_409": None,
                "prior_setup_failures": ["abort-on-container-exit terminated dependencies on successful init exit",
                    "second run output truncated and containers removed without durable evidence; excluded"],
                "conditions": {"concurrency": 2, "prefetch": 1, "acks_late": False,
                    "manual_db_mutation": False, "stale_recovery_tested": False,
                    "scenario": "natural completion then one processing-retry",
                    "published_host_ports": [], "infrastructure": ["PostgreSQL", "Redis", "AIStor"]}}
    logs = []
    try:
        code, output = run(["up", "-d", "backend", "worker"])
        logs.append("STARTUP\n" + output)
        assert code == 0, "startup failed"
        code, output = run(["run", "--name", PROJECT + "-probe", "--no-deps", "probe"], 240)
        logs.append("PROBE\n" + output)
        records = [line.split("DIAGNOSTIC_RESULT=", 1)[1] for line in output.splitlines()
                   if "DIAGNOSTIC_RESULT=" in line]
        if records:
            evidence["probe"] = json.loads(records[-1])
            evidence["classification"] = evidence["probe"]["classification"]
            evidence["reproduced_409"] = evidence["probe"].get("reproduced_409")
        else:
            evidence["failure"] = "No diagnostic result emitted; no valid reproduction claim"
        _, output = run(["logs", "--no-color", "--timestamps", "backend", "worker"])
        logs.append("BACKEND AND CELERY LIFECYCLE\n" + output)
        evidence["lifecycle_logs"] = output
        evidence["celery_event_limitation"] = "Normal worker task events OFF and result backend disabled; lifecycle is captured from worker logs, not AsyncResult PENDING."
        # Verify the source actually present in both application images without changing it.
        script = "import hashlib,json,pathlib; print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/app/app').rglob('*.py')}))"
        for service in ("backend", "worker"):
            code, output = run(["exec", "-T", service, "python", "-c", script])
            evidence[service + "_source_hashes"] = json.loads(output) if code == 0 else None
        for service in ("backend", "worker"):
            actual = evidence[service + "_source_hashes"] or {}
            evidence[service + "_source_matches_workspace"] = bool(actual) and all(
                hashlib.sha256((ROOT / "backend" / path.removeprefix("/app/")).read_bytes()).hexdigest() == digest
                for path, digest in actual.items())
        if not all(evidence.get(s + "_source_matches_workspace") for s in ("backend", "worker")):
            evidence["classification"] = "unresolved"
    except Exception as exc:
        evidence["capture_error_type"] = type(exc).__name__
    finally:
        evidence["protected_and_source_unchanged"] = before == hashes()
        evidence["protected_hashes"] = before
        evidence["production_defect_proven"] = False
        evidence["historical_cause"] = "unresolved; new completed-attempt control is not historical replay"
        evidence["saved_before_cleanup_at"] = datetime.now(timezone.utc).isoformat()
        # Exclusive creation protects every earlier artifact.
        json_path = Path(str(PREFIX) + ".json")
        log_path = Path(str(PREFIX) + ".log")
        with json_path.open("x", encoding="utf-8") as f:
            json.dump(evidence, f, indent=2)
        with log_path.open("x", encoding="utf-8") as f:
            f.write("409 diagnostic only; INGESTION RECOVERY = OPEN\n")
            f.write("Classification: " + evidence["classification"] + "\n")
            f.write("Historical cause remains unresolved. No production defect proven.\n")
            f.write("\n".join(logs))
        for path in (json_path, log_path):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            Path(str(path) + ".sha256").write_text(digest + "  " + path.name + "\n", encoding="ascii")
            print(str(path), digest, flush=True)
        print(json.dumps({k: evidence.get(k) for k in ("classification", "reproduced_409",
              "failure", "capture_error_type", "protected_and_source_unchanged")}), flush=True)
        code, _ = run(["down", "--volumes", "--remove-orphans"])
        print("isolated_cleanup_exit_code=" + str(code), flush=True)


if __name__ == "__main__":
    main()