"""Disposable matrix orchestration, immutable evidence, and regression gate."""
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected():
    paths = []
    for base in (ROOT / 'backend', ROOT / 'frontend', ROOT / 'docs'):
        for path in base.rglob('*'):
            if path.is_file() and not any(p in {'.next', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache'} for p in path.parts):
                if path.suffix not in {'.db', '.tsbuildinfo'}:
                    paths.append(path)
    paths += [p for p in ROOT.iterdir() if p.is_file()]
    for directory in ROOT.glob('diagnostic-*-evidence-*'):
        paths.extend(p for p in directory.rglob('*') if p.is_file())
    return {str(p): digest(p) for p in paths}


def run(command, env, timeout=240, cwd=ROOT):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def scenario(sid, directory):
    directory.mkdir()
    project = 'matrix-' + directory.name.lower()
    env = {**os.environ, 'MATRIX_PROJECT': project, 'MATRIX_SCENARIO': str(sid),
           'MATRIX_EVIDENCE_DIR': directory.as_posix(), 'COMPOSE_MENU': 'false'}
    cmd = ['docker', 'compose', '--ansi', 'never', '-p', project, '-f',
           str(ROOT / 'final_restart_matrix.compose.yml')]
    ev = {'scenario_id': sid, 'project': project, 'started_at': now(), 'result': 'FAIL',
          'lifecycle': [], 'evidence_directory': str(directory)}
    logs = []
    probe = None

    def compose(*args, timeout=240):
        result = run(cmd + list(args), env, timeout)
        logs.append(now() + ' ' + ' '.join(args) + '\n' + result.stdout + result.stderr)
        return result

    def states():
        result = {}
        for service in ('worker', 'backend', 'redis', 'storage-init'):
            ids = compose('ps', '-a', '-q', service).stdout.split()
            assert len(ids) == 1, 'expected exactly one isolated container: ' + service
            inspect = run(['docker', 'inspect', '--format', '{{json .State}}', ids[0]], env)
            assert inspect.returncode == 0
            result[service] = {'id': ids[0], 'state': json.loads(inspect.stdout)}
        return result

    output = (directory / 'probe.log').open('x', encoding='utf-8')
    try:
        assert compose('up', '-d', 'backend', 'worker').returncode == 0, 'isolated startup failed'
        ev['initial_lifecycle'] = states()
        # Prove images execute the exact current production Python source, read-only.
        source_code = "import pathlib,hashlib,json; print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('app').rglob('*.py')}))"
        expected = {p.relative_to(ROOT / 'backend').as_posix(): digest(p) for p in (ROOT / 'backend' / 'app').rglob('*.py')}
        for service in ('backend', 'worker'):
            r = compose('exec', '-T', service, 'python', '-c', source_code)
            assert r.returncode == 0 and json.loads(r.stdout) == expected, 'image/source mismatch: ' + service
        ev['image_source_matches'] = True
        config_code = "import json; from app.worker import celery_app as c; print(json.dumps(dict(acks_late=c.conf.task_acks_late,prefetch=c.conf.worker_prefetch_multiplier,queue=c.conf.task_default_queue)))"
        r = compose('exec', '-T', 'worker', 'python', '-c', config_code)
        ev['worker_config'] = json.loads(r.stdout)
        assert ev['worker_config'] == {'acks_late': False, 'prefetch': 1, 'queue': 'system'}
        probe = subprocess.Popen(cmd + ['run', '--no-deps', '--name', project + '-probe', 'probe'],
            cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 2100
        while probe.poll() is None:
            assert time.monotonic() < deadline, 'scenario orchestration timeout'
            for request in directory.glob('*.request'):
                if request.with_suffix('.ack').exists():
                    continue
                action = request.stem
                before = states()
                service = action.split('-', 1)[1]
                assert service in ('worker', 'backend', 'redis')
                container = before[service]['id']
                requested_at = now()
                if action == 'kill-worker':
                    command = ['docker', 'kill', '--signal=KILL', container]
                elif action == 'start-worker':
                    command = ['docker', 'start', container]
                elif action.startswith('restart-'):
                    command = ['docker', 'restart', container]
                else:
                    raise AssertionError('invalid lifecycle signal')
                r = run(command, env)
                after = states()
                ev['lifecycle'].append({'action': action, 'requested_at': requested_at,
                    'finished_at': now(), 'before': before, 'after': after, 'exit_code': r.returncode})
                assert r.returncode == 0, 'lifecycle operation failed'
                assert before['storage-init'] == after['storage-init'], 'storage initialization changed'
                for other in ('worker', 'backend', 'redis'):
                    if other != service:
                        assert before[other]['state']['StartedAt'] == after[other]['state']['StartedAt'], 'unrequested restart'
                if action == 'kill-worker':
                    assert after['worker']['state']['ExitCode'] == 137 and not after['worker']['state']['OOMKilled']
                else:
                    assert after[service]['state']['Running']
                request.with_suffix('.ack').write_text(now(), encoding='ascii')
            time.sleep(.2)
        output.flush()
        checkpoint = json.loads((directory / 'checkpoint.json').read_text(encoding='utf-8'))
        ev['probe'] = checkpoint
        r = compose('logs', '--no-color', '--timestamps', 'worker', 'backend', 'redis')
        lifecycle = r.stdout + r.stderr
        final = checkpoint.get('completion_evidence', {}).get('current_attempt_id')
        ev['receipt'] = bool(final and f'Task ingestion.process[{final}] received' in lifecycle)
        ev['celery_completed'] = bool(final and f'Task ingestion.process[{final}] succeeded' in lifecycle)
        initial = checkpoint.get('attempt_id')
        ev['initial_receipt'] = bool(initial and f'Task ingestion.process[{initial}] received' in lifecycle)
        ev['result'] = 'PASS' if checkpoint['result'] == 'PASS' and ev['receipt'] and ev['celery_completed'] and ev['initial_receipt'] else 'FAIL'
    except Exception as exc:
        ev['failure_type'] = type(exc).__name__
        ev['failure'] = str(exc)
    finally:
        if probe and probe.poll() is None:
            run(['docker', 'stop', '--time', '3', project + '-probe'], env)
            probe.kill(); probe.wait(timeout=30)
        output.close()
        if (directory / 'checkpoint.json').exists():
            ev['probe'] = json.loads((directory / 'checkpoint.json').read_text(encoding='utf-8'))
        compose('logs', '--no-color', '--timestamps', 'worker', 'backend', 'redis')
        ev['cleanup_exit_code'] = compose('down', '--volumes', '--remove-orphans').returncode
        if ev['cleanup_exit_code'] != 0:
            ev['result'] = 'FAIL'
        ev['finished_at'] = now()
        (directory / 'scenario.json').write_text(json.dumps(ev, indent=2, default=str), encoding='utf-8')
        (directory / 'lifecycle.log').write_text('\n'.join(logs), encoding='utf-8')
        for path in directory.iterdir():
            if path.is_file() and path.suffix != '.sha256':
                Path(str(path) + '.sha256').write_text(digest(path) + '  ' + path.name + '\n', encoding='ascii')
    return ev


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory = ROOT / ('final-restart-matrix-' + stamp)
    baseline = protected()
    directory.mkdir()
    ev = {'started_at': now(), 'ingestion_recovery': 'OPEN', 'scenarios': [], 'regression': {},
          'protected_before': baseline, 'scope': 'Seven independent isolated scenarios; no affected chapter access'}
    (directory / 'baseline.json').write_text(json.dumps(baseline, indent=2), encoding='utf-8')
    print(str(directory), flush=True)
    ids = [int(v) for v in sys.argv[1:]] or list(range(1, 8))
    for sid in ids:
        result = scenario(sid, directory / f'scenario-{sid}')
        ev['scenarios'].append(result)
        print(f'scenario {sid}: {result["result"]}', flush=True)
        (directory / 'progress.json').write_text(json.dumps(ev, indent=2), encoding='utf-8')
    env = dict(os.environ)
    env.pop('TEST_DATABASE_URL', None); env.pop('ALLOW_TEST_DB_RESET', None)
    env.update(APP_ENV='test', DATABASE_URL='sqlite:///:memory:', REDIS_URL='redis://localhost:6379/15',
        S3_ENDPOINT_URL='http://localhost:9000', S3_ACCESS_KEY='test', S3_SECRET_KEY='secret')
    python = str(ROOT / '.venv/Scripts/python.exe')
    tests = ['tests/test_ingestion_local.py', 'tests/test_ingestion_recovery_v1.py',
             'tests/test_ingestion_recovery_acceptance.py', 'tests/test_ingestion_legacy_migration.py']
    commands = [('focused', [python, '-m', 'pytest', '-q', *tests], ROOT / 'backend'),
                ('full_backend', [python, '-m', 'pytest', '-q', 'tests'], ROOT / 'backend'),
                ('ruff', [python, '-m', 'ruff', 'check', str(ROOT / 'backend')], ROOT),
                ('typecheck', ['cmd.exe', '/c', 'npm run typecheck'], ROOT / 'frontend'),
                ('build', ['cmd.exe', '/c', 'npm run build'], ROOT / 'frontend')]
    for name, command, cwd in commands:
        started = now()
        try:
            r = run(command, env, 1800, cwd)
            output, code = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            output, code = 'Regression timed out', -1
        path = directory / (name + '.log'); path.write_text(output, encoding='utf-8')
        ev['regression'][name] = {'started_at': started, 'finished_at': now(), 'exit_code': code,
            'result': 'PASS' if code == 0 else 'FAIL', 'log': str(path), 'sha256': digest(path)}
        print(name + ': ' + ev['regression'][name]['result'], flush=True)
    ev['protected_unchanged'] = all(Path(p).exists() and digest(Path(p)) == h for p, h in baseline.items())
    ev['changed_protected'] = [p for p, h in baseline.items() if not Path(p).exists() or digest(Path(p)) != h]
    ev['finished_at'] = now()
    ev['ingestion_recovery'] = 'PASS' if ids == list(range(1, 8)) and all(s['result'] == 'PASS' for s in ev['scenarios']) and all(r['result'] == 'PASS' for r in ev['regression'].values()) and ev['protected_unchanged'] else 'OPEN'
    report = directory / 'acceptance.json'
    report.write_text(json.dumps(ev, indent=2), encoding='utf-8')
    log = directory / 'acceptance.log'
    log.write_text('INGESTION RECOVERY = ' + ev['ingestion_recovery'] + '\n' +
        '\n'.join(f'Scenario {s["scenario_id"]}: {s["result"]}; {s["evidence_directory"]}' for s in ev['scenarios']) + '\n' +
        '\n'.join(f'{name}: {r["result"]}; {r["log"]}' for name, r in ev['regression'].items()) + '\n' +
        'Protected unchanged: ' + str(ev['protected_unchanged']), encoding='utf-8')
    for path in (report, log):
        Path(str(path) + '.sha256').write_text(digest(path) + '  ' + path.name + '\n', encoding='ascii')
        print(str(path) + ' ' + digest(path), flush=True)


if __name__ == '__main__':
    main()