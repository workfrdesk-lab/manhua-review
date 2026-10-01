"""Preserve the failed isolated completion run without attempting recovery again."""
import hashlib
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
PROJECT = 'diagstale-20261001t093945908749z'
EVIDENCE = ROOT / 'diagnostic-stale-evidence-20261001T093945908749Z'
PREFIX = ROOT / ('diagnostic-stale-completion-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))

def docker(*args):
    r = subprocess.run(['docker', *args], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=90)
    return {'command': list(args), 'exit_code': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}

def main():
    captures = []
    for service in ('worker', 'backend', 'redis', 'storage-init', 'migrate'):
        name = PROJECT + '-' + service + '-1'
        captures.append(docker('inspect', '--format', '{{json .State}}', name))
        captures.append(docker('logs', '--timestamps', name))
    captures.append(docker('logs', PROJECT + '-probe'))
    # Stop only the diagnostic observer; never start the replacement worker.
    captures.append(docker('stop', '--time', '5', PROJECT + '-probe'))
    checkpoint = json.loads((EVIDENCE / 'checkpoint.json').read_text(encoding='utf-8'))
    previous = json.loads((ROOT / 'diagnostic-ingestion-stale-20261001T091521214512Z.json').read_text(encoding='utf-8'))
    protected = previous.get('protected_hashes', {})
    unchanged = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest().lower() == h.lower()
                 for p, h in protected.items()}
    evidence = {'classification': 'partial-evidence-not-pass', 'ingestion_recovery': 'OPEN',
        'project': PROJECT, 'probe': checkpoint, 'captures': captures,
        'checks': {'worker_crash': True, 'natural_lease_expiry': checkpoint.get('natural_lease_expiry', False),
                   'stale_recovery': checkpoint.get('stale_recovery_succeeded', False),
                   'replacement_worker_receipt': False, 'replacement_completion': False,
                   'db_aistor_consistency': False, 'restart_persistence': False},
        'readiness_failure': 'compose start worker restarted completed dependencies; storage-init exited 1; worker remained exited 137',
        'capture_failure': (ROOT / 'diagnostic-stale-completion-launch.err').read_text(encoding='utf-8'),
        'protected_hash_comparisons': unchanged,
        'no_retry_after_readiness_failure': True}
    log = '\n'.join(json.dumps(c, indent=2) for c in captures)
    # Save before deleting any disposable resources.
    json_path, log_path = Path(str(PREFIX) + '.json'), Path(str(PREFIX) + '.log')
    json_path.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    log_path.write_text(log, encoding='utf-8')
    ids = docker('ps', '-a', '-q', '--filter', 'label=com.docker.compose.project=' + PROJECT)['stdout'].split()
    evidence['cleanup'] = docker('rm', '-f', '-v', *ids) if ids else None
    evidence['network_cleanup'] = docker('network', 'rm', PROJECT + '_default')
    json_path.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    for path in (json_path, log_path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        Path(str(path) + '.sha256').write_text(digest + '  ' + path.name + '\n', encoding='ascii')
        print(str(path), digest)

if __name__ == '__main__':
    main()