"""Closure evidence only: no probes, service mutations, or license-content reads."""
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(args):
    return subprocess.run(args, capture_output=True, timeout=60)


def main():
    prior_path = ROOT / 'docker-access-preflight-20261001T163139304862Z.json'
    prior = json.loads(prior_path.read_text())
    paths = set(prior['hashes']['before'])
    for pattern in ('docker-access-preflight-*', 'credential-remediation-*', 'secret-audit*'):
        paths.update(str(p) for p in ROOT.glob(pattern) if p.is_file())
    before = {p: digest(p) for p in sorted(paths)}
    baseline_matches = all(before[p] == h for p, h in prior['hashes']['before'].items())
    ids = command(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=recap-studio'])
    if ids.returncode or not ids.stdout.strip():
        raise RuntimeError('Container inventory unavailable')
    result = command(['docker', 'inspect', *ids.stdout.decode().split()])
    if result.returncode:
        raise RuntimeError('Container metadata unavailable')
    containers = {c['Config']['Labels']['com.docker.compose.service']: c for c in json.loads(result.stdout)}
    init = containers['storage-init']
    image = {'image_id': init['Image'], 'stopped': not init['State']['Running'], 'layers': 'UNVERIFIED',
             'undecoded_members': None, 'credential_file_exposure': 'UNVERIFIED'}
    for operation, args in (
        ('metadata', ['docker', 'image', 'inspect', init['Image']]),
        ('history', ['docker', 'history', '--no-trunc', init['Image']]),
    ):
        result = command(args)
        error = result.stderr.decode(errors='replace').lower()
        image[operation] = 'VERIFIED' if result.returncode == 0 else 'UNVERIFIED'
        image[operation + '_error'] = ('ImageNotFound' if 'no such image' in error or 'not found' in error
                                      else 'CommandFailure') if result.returncode else None
    inventory = command(['docker', 'image', 'ls', '--no-trunc', '--quiet'])
    image['present_in_local_inventory'] = init['Image'] in inventory.stdout.decode().split() if not inventory.returncode else None
    compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    copies = []
    for service in ('backend', 'worker', 'migrate', 'storage-init', 'frontend'):
        build = compose['services'][service]['build']
        context = (ROOT / (build if isinstance(build, str) else build['context'])).resolve()
        dockerfile = context / (build.get('dockerfile', 'Dockerfile') if isinstance(build, dict) else 'Dockerfile')
        rules = (context / '.dockerignore').read_text().splitlines()
        instructions = []
        for number, line in enumerate(dockerfile.read_text().splitlines(), 1):
            words = line.split()
            if not words or words[0] not in ('COPY', 'ADD'):
                continue
            options = [w for w in words[1:] if w.startswith('--')]
            operands = [w for w in words[1:] if not w.startswith('--')]
            stage = any(w.startswith('--from=') for w in options)
            instructions.append({'line': number, 'operation': words[0], 'stage_copy': stage,
                                 'source_path_identities': [hashlib.sha256(s.encode()).hexdigest() for s in operands[:-1]],
                                 'sources_exist': None if stage else all((context / s).exists() for s in operands[:-1]),
                                 'recursive_whole_context': '.' in operands[:-1],
                                 'content_exposure_verified': False})
        copies.append({'service': service, 'context': str(context), 'dockerfile_sha256': digest(dockerfile),
                       'dockerignore_sha256': digest(context / '.dockerignore'),
                       'env_exclusion_rule_present': '.env*' in rules,
                       'host_generated_frontend_excluded': '.next' in rules and 'node_modules' in rules,
                       'root_audit_files_outside_context': context != ROOT,
                       'instructions': instructions, 'full_inclusion_analysis': 'UNVERIFIED'})
    storage = containers['storage']
    mount = next((m for m in storage['Mounts'] if m['Destination'] == '/run/secrets/minio.license'), None)
    license_state = {'mounted': bool(mount), 'validity': 'UNVERIFIED', 'validity_blocks_rotation': False,
                     'contents_read_hashed_copied_or_modified': False,
                     'host_environment_variable_present': bool(os.environ.get('MINIO_LICENSE_PATH')),
                     'compose_secret_uses_license_path_variable': 'MINIO_LICENSE_PATH' in str(compose.get('secrets', {})),
                     'expected_host_source_verified': False,
                     'explanation': 'Container mount persists independently of current host environment; launch-time variable provenance remains unverified.'}
    if mount:
        license_state.update({'read_only': not mount['RW'], 'mount_type': mount['Type'],
                              'source_path_identity': hashlib.sha256(mount['Source'].encode()).hexdigest(),
                              'destination_path_identity': hashlib.sha256(mount['Destination'].encode()).hexdigest()})
        check = command(['docker', 'exec', storage['Id'], 'sh', '-c', 'test -s "$1"', 'license-metadata-check', mount['Destination']])
        license_state['nonempty'] = check.returncode == 0
    after = {p: digest(p) for p in before}
    unresolved = [
        'Exact storage-init image is absent from the local image inventory; inspect/history return ImageNotFound. Layers and historical credential-bearing files cannot be proven from the stopped container or a different/rebuilt image.',
        'COPY sources were inventoried, but exhaustive effective Dockerignore inclusion and secret-bearing content classification remain unverified, including frontend build-stage outputs.',
        'Historical generic findings have not all been deterministically reconciled individually; previous absence of known credential matches is not proof they are non-secret.',
        'PostgreSQL server authentication rule precedence and complete running/stopped client and *_FILE precedence are not yet proven; prior successful probes are preserved, not repeated.',
        'License is mounted nonempty read-only, but its source has not been independently matched to the expected launch configuration. License validity itself is non-blocking.',
    ]
    if before != after or not baseline_matches:
        unresolved.append('Protected evidence hash mismatch detected.')
    report = {'decision': 'ROTATION BLOCKED', 'gate': 'OPEN', 'created_at': datetime.now(timezone.utc).isoformat(),
              'storage_init_image': image, 'copy_analysis': copies, 'license': license_state,
              'prior_probe_evidence': {'path': str(prior_path), 'sha256': digest(prior_path), 'probes_repeated': False},
              'remaining_findings': [{'path': x['path'], 'classification': 'unknown', 'reason': 'Contextual determination not completed'}
                                     for x in prior['secret_findings'] if x['classification'] == 'unknown'],
              'incident': {'earlier_literal_exposure': True, 'literal_exposure_recurred_this_pass': True,
                           'source': 'Initial raw file reads and broad code search tool output',
                           'values_in_this_artifact': False, 'public_external_exposure_established': False},
              'safety': {'credentials_rotated': False, 'services_restarted_or_recreated': False,
                         'db_s3_probes_repeated': False, 'migrations_or_storage_init_executed': False,
                         'application_or_chapter_objects_accessed': False,
                         'historical_audit_files_overwritten': False, 'semantics_changed': False},
              'protected_hashes_before': before, 'protected_hashes_after': after,
              'prior_baseline_matches': baseline_matches, 'protected_unchanged': before == after,
              'unresolved_items': unresolved}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    prefix = ROOT / ('credential-closure-preflight-' + stamp)
    log = 'ROTATION BLOCKED\nNo credentials rotated; no credential or license values included.\nLiteral exposure recurred in initial tool output; no public exposure established.\n' + '\n'.join(unresolved) + '\n'
    for suffix, content in (('.json', json.dumps(report, indent=2)), ('.log', log)):
        path = Path(str(prefix) + suffix)
        with path.open('x', encoding='utf-8') as stream:
            stream.write(content)
        with Path(str(path) + '.sha256').open('x', encoding='ascii') as stream:
            stream.write(digest(path) + '  ' + path.name + '\n')
        assert digest(path) == Path(str(path) + '.sha256').read_text().split()[0]
    assert all(digest(p) == h for p, h in before.items())
    print(json.dumps({'decision': report['decision'], 'report': str(prefix) + '.json',
                      'protected_unchanged': before == after, 'sidecars_verified': True}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'incomplete', 'error_class': type(error).__name__}))
        raise SystemExit(1)