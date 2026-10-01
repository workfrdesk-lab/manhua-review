"""Capture exactly one natural stale-attempt recovery run."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
STAMP = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
PROJECT = 'diagstale-' + STAMP.lower()
EVIDENCE = ROOT / ('diagnostic-stale-evidence-' + STAMP)
EVIDENCE.mkdir()
PREFIX = ROOT / ('diagnostic-stale-completion-' + STAMP)
CMD = ['docker', 'compose', '--ansi', 'never', '-p', PROJECT,
       '-f', str(ROOT / 'diagnostic-stale.compose.yml')]
ENV = {**os.environ, 'COMPOSE_MENU': 'false', 'COMPOSE_PROGRESS': 'plain',
       'DIAGNOSTIC_EVIDENCE_DIR': EVIDENCE.as_posix()}
PROTECTED = [p for p in ROOT.iterdir() if p.is_file() and
             (p.suffix in ('.json', '.log') or p.name == 'final_acceptance_runner.py')]
PROTECTED += list((ROOT / 'backend').rglob('*.py'))


def file_hashes(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def run(args, timeout=180):
    return subprocess.run(CMD + args, capture_output=True, text=True,
        encoding='utf-8', errors='replace', env=ENV, timeout=timeout)


def main():
    before = file_hashes(PROTECTED)
    logs = []
    evidence = {'classification': 'unresolved', 'ingestion_recovery': 'OPEN',
        'project': PROJECT, 'conditions': {'manual_db_mutation': False,
        'manual_lease_expiry': False, 'fake_broker_duplicate': False,
        'acks_late': False, 'prefetch': 1, 'concurrency': 2,
        'infrastructure': ['PostgreSQL', 'Redis', 'AIStor'],
        'scenario': 'real worker kill, natural lease expiry, one stale recovery'},
        'evidence_directory': str(EVIDENCE)}
    probe = None
    probe_output = (EVIDENCE / 'probe-output.log').open('w', encoding='utf-8')
    def container_state(service):
        ids = run(['ps', '-a', '-q', service]).stdout.split()
        assert len(ids) == 1, 'Expected exactly one isolated ' + service
        r = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', ids[0]],
                           capture_output=True, text=True, timeout=30, check=True)
        return ids[0], json.loads(r.stdout)
    def diagnostics(label):
        r = run(['ps', '-a', '--format', 'json'])
        logs.append(label + ' CONTAINER STATUS\n' + r.stdout + r.stderr)
        for service in ('worker', 'backend', 'redis'):
            ids = run(['ps', '-a', '-q', service]).stdout.split()
            if ids:
                result = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', *ids],
                    capture_output=True, text=True, timeout=30)
                logs.append(label + ' ' + service + ' STATE/HEALTH\n' + result.stdout + result.stderr)
                if label == 'kill' and service == 'worker':
                    evidence['worker_kill_state'] = json.loads(result.stdout)
        r = run(['logs', '--no-color', '--timestamps', 'worker', 'backend', 'redis'])
        logs.append(label + ' LOGS\n' + r.stdout + r.stderr)
        return r.stdout + r.stderr
    try:
        r = run(['up', '-d', 'backend', 'worker'])
        logs.append('STARTUP\n' + r.stdout + r.stderr)
        if r.returncode:
            raise RuntimeError('startup failed')
        probe = subprocess.Popen(CMD + ['run', '--name', PROJECT + '-probe', '--no-deps', 'probe'],
            stdout=probe_output, stderr=subprocess.STDOUT, text=True,
            encoding='utf-8', errors='replace', env=ENV)
        while probe.poll() is None:
            request = next((p for p in EVIDENCE.glob('*.request') if not p.with_suffix('.ack').exists()), None)
            if request:
                action = request.stem
                if action == 'kill':
                    r = run(['kill', 'worker'])
                elif action == 'start':
                    init_before = container_state('storage-init')[1]
                    worker_id, _ = container_state('worker')
                    r = subprocess.run(['docker', 'start', worker_id], capture_output=True,
                                       text=True, timeout=60)
                    evidence['replacement_container_state'] = container_state('worker')[1]
                    evidence['storage_init_unchanged_on_start'] = init_before == container_state('storage-init')[1]
                    assert evidence['replacement_container_state']['Running'], 'worker not running'
                    assert evidence['storage_init_unchanged_on_start'], 'storage-init restarted unexpectedly'
                elif action in ('restart-worker', 'restart-backend', 'restart-redis'):
                    r = run(['restart', '--no-deps', action.split('-', 1)[1]])
                else:
                    raise RuntimeError('unknown probe signal')
                logs.append(action.upper() + '\n' + r.stdout + r.stderr)
                diagnostics(action)
                if r.returncode:
                    raise RuntimeError('Lifecycle command failed: ' + action)
                request.with_suffix('.ack').write_text(datetime.now(timezone.utc).isoformat(), encoding='ascii')
            time.sleep(.2)
        probe_output.flush()
        output = (EVIDENCE / 'probe-output.log').read_text(encoding='utf-8')
        logs.append('PROBE\n' + output)
        if (EVIDENCE / 'checkpoint.json').exists():
            evidence['probe'] = json.loads((EVIDENCE / 'checkpoint.json').read_text(encoding='utf-8'))
            evidence['classification'] = evidence['probe']['classification']
        else:
            evidence['failure'] = 'No diagnostic result emitted'
        r = run(['logs', '--no-color', '--timestamps', 'backend', 'worker'], timeout=240)
        logs.append('BACKEND AND WORKER LIFECYCLE\n' + r.stdout + r.stderr)
        evidence['lifecycle_logs'] = r.stdout + r.stderr
        evidence['worker_kill_signal_observed'] = (EVIDENCE / 'kill.ack').exists()
        evidence['worker_restart_signal_observed'] = (EVIDENCE / 'restart-worker.ack').exists()
        evidence['worker_replacement_start_observed'] = (EVIDENCE / 'start.ack').exists()
    except Exception as exc:
        evidence['capture_error_type'] = type(exc).__name__
        evidence['capture_error_detail'] = str(exc)
    finally:
        if probe and probe.poll() is None:
            subprocess.run(['docker', 'stop', '--time', '5', PROJECT + '-probe'],
                           capture_output=True, timeout=30)
            probe.kill()
            probe.wait(timeout=30)
        probe_output.close()
        lifecycle = diagnostics('FINAL BEFORE CLEANUP')
        if (EVIDENCE / 'checkpoint.json').exists():
            evidence['probe'] = json.loads((EVIDENCE / 'checkpoint.json').read_text(encoding='utf-8'))
        p = evidence.get('probe', {})
        attempt = p.get('replacement_attempt_id')
        receipt = bool(attempt and ('Task ingestion.process[' + attempt + '] received') in lifecycle)
        evidence['replacement_worker_receipt'] = receipt
        evidence['checks'] = {
            'worker_crash': evidence.get('worker_kill_state', {}).get('ExitCode') == 137 and p.get('real_interruption', False),
            'natural_lease_expiry': p.get('natural_lease_expiry', False),
            'stale_recovery': p.get('stale_recovery_succeeded', False),
            'replacement_worker_receipt': receipt,
            'replacement_completion': p.get('replacement_completed', False),
            'db_aistor_consistency': p.get('db_aistor_consistent', False),
            'restart_persistence': p.get('restart_persistence_verified', False)}
        evidence['classification'] = ('single-natural-stale-completion-passed-not-final-acceptance'
            if all(evidence['checks'].values()) else 'partial-evidence-not-pass')
        evidence['protected_and_source_unchanged'] = before == file_hashes(PROTECTED)
        evidence['protected_hashes'] = before
        evidence['production_defect_proven'] = False
        evidence['saved_before_cleanup_at'] = datetime.now(timezone.utc).isoformat()
        evidence['logs_saved_before_cleanup'] = True
        json_path, log_path = Path(str(PREFIX) + '.json'), Path(str(PREFIX) + '.log')
        json_path.write_text(json.dumps(evidence, indent=2, default=str), encoding='utf-8')
        log_path.write_text('Single natural stale recovery diagnostic; INGESTION RECOVERY = OPEN\n' +
            'Classification: ' + evidence['classification'] + '\n' + '\n'.join(logs), encoding='utf-8')
        for path in (json_path, log_path):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            Path(str(path) + '.sha256').write_text(digest + '  ' + path.name + '\n', encoding='ascii')
            print(str(path), digest, flush=True)
        r = run(['down', '--volumes', '--remove-orphans'])
        print('isolated_cleanup_exit_code=' + str(r.returncode), flush=True)
        # Preserve raw checkpoints and lifecycle signals, including failed runs.


if __name__ == '__main__':
    main()