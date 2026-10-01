"""Value-free, read-only credential preflight. Never runs rotation commands."""
import base64
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
SENSITIVE = re.compile(r'PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY|ROOT_USER|DATABASE_URL|REDIS_URL', re.I)
PATTERN = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{32,}')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def capture(args, env=None):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError('command failed; output suppressed')
    return result.stdout


def main():
    global stage
    stage = 'initialization'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    report = {'gate': 'OPEN', 'phase': 'pre-rotation review', 'rotation_performed': False,
              'public_exposure': 'not established', 'git_history': 'unavailable: no repository',
              'secret_values_exposed_in_tool_output': True,
              'disclosure': 'Raw preflight reads exposed active local-stack and disposable fixture literals. Values are not reproduced in this report.',
              'findings': [], 'runtime': [], 'images': [], 'unresolved': [], 'prior_audits': []}
    stage = 'docker container inventory'
    ids = capture(['docker', 'ps', '-aq']).decode().split()
    containers = json.loads(capture(['docker', 'inspect', *ids])) if ids else []
    containers = sorted((c for c in containers if c['Name'].startswith('/recap-studio-')), key=lambda c: c['Name'])
    needles = {}

    def register(key, value, service, running):
        if not SENSITIVE.search(key) or not value:
            return
        if key.upper().endswith(('_FILE', '_PATH')):
            report.setdefault('noncredential_runtime_references', []).append({
                'service': service, 'key': key, 'classification': 'scanner false positive',
                'reason': 'File/path configuration reference, not credential contents'})
            return
        if 'URL' in key.upper():
            value = unquote(urlsplit(value).password or '')
        if not value:
            return
        raw = value.encode()
        fp = 'sha256:' + digest(raw)[:16]
        entry = needles.setdefault(raw, {'fingerprint': fp, 'runtime_references': [],
                                        'classification': 'unknown'})
        if running:
            entry['classification'] = 'active runtime credential'
            entry['authentication_verified'] = False
        entry['runtime_references'].append({'service': service, 'key': key, 'running': running})

    license_bytes = b''
    stage = 'runtime environment metadata'
    for c in containers:
        service = c['Config'].get('Labels', {}).get('com.docker.compose.service', 'unknown')
        for item in c['Config'].get('Env', []):
            key, _, value = item.partition('=')
            register(key, value, service, c['State']['Running'])
        report['runtime'].append({'service': service, 'running': c['State']['Running'],
                                  'health': c['State'].get('Health', {}).get('Status', 'not configured'),
                                  'image_id': c['Image']})
        if service == 'storage':
            for mount in c['Mounts']:
                if mount['Destination'].endswith('/minio.license'):
                    # Docker Desktop source paths may not be native Windows paths.
                    license_bytes = capture(['docker', 'exec', c['Id'], 'cat', mount['Destination']]).strip()
                    report['license'] = {'configured': True, 'nonempty': bool(license_bytes),
                                         'rotation_target': False, 'validity': 'not independently verified'}
    variants = {raw: [raw, base64.b64encode(raw), raw.decode().encode('utf-16-le')] for raw in needles}

    def hits(data):
        return sorted(entry['fingerprint'] for raw, entry in needles.items()
                      if any(v in data for v in variants[raw]))

    stage = 'workspace value-free scan'
    before = {}
    count = 0
    for directory, dirs, files in os.walk(ROOT):
        dirs.sort()
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                report['unresolved'].append({'path': str(path), 'reason': 'symlink not followed'})
                continue
            try:
                data = path.read_bytes()
            except OSError:
                report['unresolved'].append({'path': str(path), 'reason': 'unreadable'})
                continue
            count += 1
            rel = path.relative_to(ROOT)
            generated = path.suffix in {'.log', '.out', '.err', '.json', '.db', '.pyc', '.xml'} or any(
                part in {'.next', '__pycache__', '.venv', 'node_modules'} for part in rel.parts)
            if '.venv' not in rel.parts and 'node_modules' not in rel.parts and '.next' not in rel.parts:
                before[str(path)] = digest(data)
            matched = hits(data)
            generic = bool(PATTERN.search(data))
            license_hit = bool(license_bytes and license_bytes in data)
            if matched or generic or license_hit:
                report['findings'].append({'path': str(path), 'fingerprints': matched,
                    'classification': 'generated/local audit artifact' if generated else (
                        'active runtime credential' if matched else 'unknown'),
                    'classification_basis': 'Exact runtime value match; authentication and contextual review pending' if matched else 'Pattern match only; contextual review pending',
                    'generic_pattern_match': generic, 'license_material_match': license_hit,
                    'source_or_build_input': rel.parts[0] in {'backend', 'frontend'} and not generated,
                    'unresolved': True})
            if name.startswith(('secret-audit', 'fresh-secret-audit')):
                report['prior_audits'].append({'path': str(path), 'sha256': digest(data),
                                              'active_fingerprints_found': matched})
    report['files_scanned'] = count
    report['credentials'] = sorted(needles.values(), key=lambda x: x['fingerprint'])
    report['confirmed_fingerprints_present'] = {fp: any(v['fingerprint'] == fp for v in needles.values())
        for fp in ['sha256:ed20191044553dac', 'sha256:ad14927ac93ea513']}
    stage = 'runtime log checks'
    for c in containers:
        logs = capture(['docker', 'logs', c['Id']])
        # docker logs writes some service logs to stderr; capture both separately below.
        result = subprocess.run(['docker', 'logs', c['Id']], capture_output=True, timeout=180)
        report.setdefault('log_checks', []).append({'container_id': c['Id'],
            'fingerprints': hits(logs + result.stderr), 'exit_code': result.returncode,
            'license_material_match': bool(license_bytes and license_bytes in logs + result.stderr)})
    stage = 'image metadata checks'
    for image in sorted({c['Image'] for c in containers}):
        try:
            metadata = capture(['docker', 'image', 'inspect', image])
            history = capture(['docker', 'history', '--no-trunc', '--format', '{{json .}}', image])
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            report['images'].append({'image_id': image, 'coverage': 'unavailable; command failed'})
            report['unresolved'].append({'image_id': image, 'reason': 'Metadata/history command failed; output suppressed'})
            continue
        report['images'].append({'image_id': image, 'metadata_fingerprints': hits(metadata),
            'history_fingerprints': hits(history), 'layers': 'not yet inspected',
            'license_match': bool(license_bytes and (license_bytes in metadata or license_bytes in history))})
    report['unresolved'] += [
        {'reason': 'Image layers, effective build-context exclusion rules, and generic matches require further inspection.'},
        {'reason': 'Runtime environment references are confirmed; authenticated DB/S3 probes and effective application settings remain unverified.'},
        {'reason': 'Compose rendering fails without MINIO_LICENSE_PATH in host invocation; reconcile with running license mount without altering license.'},
        {'reason': 'Prior broad scanner findings need line-level classification; keyword-only hits must not be treated as credentials.'},
        {'reason': 'Public exposure, remote repository history, and external distribution cannot be determined from this workspace.'},
        {'reason': 'Rotation and post-rotation tests have not run; approval and prerequisite checks are required.'}]
    report['rotation_plan'] = [
        'Before approval: prove DB password authentication over TCP and S3 authenticated head_bucket using effective backend/worker settings; no chapter or object access.',
        'Reconcile Compose variables and running services, including stopped migrate and storage-init; preserve license bytes, mount, command, image and data volumes.',
        'Inspect image layers and build exclusion rules; classify all unknowns and prior findings. Hash protected evidence and ingestion source before changes.',
        'After reviewed approval: use cryptographic randomness and restricted local secret storage outside build contexts; never pass values through command-line arguments or rendered logs.',
        'Schedule a maintenance window, block new work, and verify worker active/reserved/scheduled tasks are empty without exposing payloads. Abort rather than interrupt affected chapter work.',
        'Rotate PostgreSQL role password using a secure authenticated channel with server/client statement logging assessed. Updating POSTGRES_PASSWORD alone does not rotate an existing volume.',
        'Update PostgreSQL bootstrap configuration plus backend, worker and migrate DB settings together; preserve role, DB and volume. Verify new password succeeds and old password fails.',
        'Rotate AIStor root access ID and secret and update backend, worker, migrate and storage-init references. Preserve license configuration and storage volume; do not run bucket-policy initialization blindly.',
        'Recreate only required services with no dependency cascades or volume deletion. Refresh stopped one-shot service configuration without executing migrations or storage-init mutations.',
        'Do not restart Redis or change Celery settings; verify Redis ping and Celery control ping. Verify application health, DB SELECT 1 and S3 head_bucket with new credentials.',
        'Remove active credential literals/defaults only from mutable configuration/examples/build inputs; preserve protected historical artifacts and classify them as containing revoked credentials.',
        'On failure keep gate OPEN, preserve data and evidence, and stop dependent clients if inconsistent. Any rollback of credentials must be explicit and recorded, never a silent fallback.',
        'Run post-rotation audit, backend and focused recovery tests, frontend typecheck/build and runtime health/log checks; no seven-scenario rerun absent semantic changes.'
    ]
    report['after_classification'] = 'unchanged; no rotation or remediation performed'
    report['protected_baseline'] = before
    report['protected_unchanged'] = all(Path(p).exists() and digest(Path(p).read_bytes()) == h for p, h in before.items())
    report['ingestion_semantics_changed'] = False
    report['affected_chapter_accessed'] = False
    report['phase_4b_started'] = False
    stage = 'redacted evidence write'
    prefix = ROOT / ('credential-remediation-preflight-' + stamp)
    output = json.dumps(report, indent=2).encode()
    log = ('CREDENTIAL REMEDIATION = OPEN\nPre-rotation evidence only. No credentials rotated.\n'
           'Secret literals were exposed in initial tool reads; no values reproduced in these artifacts.\n'
           'Public exposure not established. License not rotated.\n' +
           '\n'.join(report['rotation_plan']) + '\n').encode()
    # Fail closed if any known credential/license bytes would be emitted.
    for payload in (output, log):
        if any(raw in payload for raw in needles) or (license_bytes and license_bytes in payload):
            raise RuntimeError('report safety check failed; output suppressed')
    for suffix, payload in [('.json', output), ('.log', log)]:
        path = Path(str(prefix) + suffix)
        with path.open('xb') as stream:
            stream.write(payload)
        with Path(str(path) + '.sha256').open('x', encoding='ascii') as stream:
            stream.write(digest(payload) + '  ' + path.name + '\n')
    print(json.dumps({'gate': 'OPEN', 'report': str(prefix) + '.json', 'files_scanned': count,
                      'findings': len(report['findings']), 'protected_unchanged': report['protected_unchanged'],
                      'confirmed_fingerprints_present': report['confirmed_fingerprints_present']}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Preflight incomplete; stage=' + stage + '; exception class=' + type(error).__name__)
        raise SystemExit(1)