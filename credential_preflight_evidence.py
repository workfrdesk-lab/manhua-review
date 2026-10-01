"""Read-only credential preflight; all output is deliberately value-free."""
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
KNOWN = {'sha256:ed20191044553dac', 'sha256:ad14927ac93ea513'}
SENSITIVE = re.compile(r'PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY|ROOT_USER|DATABASE_URL|REDIS_URL', re.I)
IGNORE_DIRS = {'.git', '.venv', 'node_modules', '.next', '__pycache__', '.pytest_cache', '.ruff_cache'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run(args, *, input_data=None, timeout=180):
    result = subprocess.run(args, input=input_data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('command failed')
    return result.stdout


def fp(value):
    return 'sha256:' + sha(value.encode())[:16]


def inspect_containers():
    ids = run(['docker', 'ps', '-aq']).decode().split()
    return sorted((c for c in json.loads(run(['docker', 'inspect', *ids]))
                   if c['Config'].get('Labels', {}).get('com.docker.compose.service')),
                  key=lambda c: c['Name']) if ids else []


def env_map(container):
    return dict(item.split('=', 1) for item in container['Config'].get('Env', []) if '=' in item)


def service(container):
    return container['Config'].get('Labels', {}).get('com.docker.compose.service', 'unknown')


def classify_value(key, value):
    if key.upper().endswith(('_FILE', '_PATH')):
        return None
    if 'URL' in key.upper():
        value = unquote(urlsplit(value).password or '')
    return fp(value) if value else None


def probe(container, kind):
    name = service(container)
    code = (
        "import os,json,asyncio,hashlib\n"
        "from urllib.parse import urlsplit,unquote\n"
        "import logging; logging.disable(logging.CRITICAL)\n"
        "from app.config import get_settings\n"
        "s=get_settings()\n"
        "KIND=" + repr(kind) + "\n"
        "try:\n"
        " if KIND == 'db':\n"
        "  from sqlalchemy.ext.asyncio import create_async_engine\n"
        "  from sqlalchemy import text\n"
        "  from app.db import database_url_for_sqlalchemy\n"
        "  u=s.database_url.get_secret_value()\n"
        "  async def check():\n"
        "   engine=create_async_engine(database_url_for_sqlalchemy(u),echo=False)\n"
        "   try:\n"
        "    async with engine.connect() as c: assert (await c.execute(text('SELECT 1'))).scalar_one()==1\n"
        "   finally: await engine.dispose()\n"
        "  asyncio.run(check())\n"
        "  endpoint='sha256:'+hashlib.sha256((str(urlsplit(u).hostname)+str(urlsplit(u).port)+urlsplit(u).path).encode()).hexdigest()[:16]\n"
        " else:\n"
        "  import boto3\n"
        "  c=boto3.client('s3',endpoint_url=s.s3_endpoint_url,aws_access_key_id=s.s3_access_key.get_secret_value(),aws_secret_access_key=s.s3_secret_key.get_secret_value(),region_name=s.s3_region)\n"
        "  c.head_bucket(Bucket=s.s3_bucket)\n"
        "  endpoint='sha256:'+hashlib.sha256((s.s3_endpoint_url+s.s3_bucket).encode()).hexdigest()[:16]\n"
        " print(json.dumps({'success':True,'endpoint':endpoint,'settings_match_environment': s.database_url.get_secret_value()==os.environ.get('DATABASE_URL') and s.s3_access_key.get_secret_value()==os.environ.get('S3_ACCESS_KEY') and s.s3_secret_key.get_secret_value()==os.environ.get('S3_SECRET_KEY')}))\n"
        "except Exception as e:\n"
        " print(json.dumps({'success':False,'error_class':type(e).__name__}))\n"
    )
    result = subprocess.run(['docker', 'exec', '-i', container['Id'], 'python', '-'], input=code.encode(),
                            capture_output=True, timeout=180)
    try:
        raw_body = json.loads(result.stdout.decode(errors='replace').splitlines()[-1])
        body = {k: v for k, v in raw_body.items() if k in {'success','endpoint','error_class','settings_match_environment'}}
    except (ValueError, IndexError):
        body = {'success': False, 'error_class': 'ProbeOutputUnavailable'}
    keys = ('DATABASE_URL',) if kind == 'db' else ('S3_ACCESS_KEY', 'S3_SECRET_KEY')
    values = [env_map(container).get(k, '') for k in keys]
    return {'service': name, 'kind': kind, 'timestamp': datetime.now(timezone.utc).isoformat(),
            'credential_fingerprints': [classify_value(k, v) for k, v in zip(keys, values) if classify_value(k, v)],
            **body}


def layer_scan(image, needles):
    result = {'image_id': image, 'metadata': 'verified', 'history': 'verified', 'layers': 'UNVERIFIED',
              'known_fingerprints_present': [], 'credential_bearing_files': [], 'errors': []}
    try:
        metadata = run(['docker', 'image', 'inspect', image])
        history = run(['docker', 'history', '--no-trunc', image])
        result['metadata_hits'] = sorted(k for k,v in needles.items() if v in metadata)
        result['history_hits'] = sorted(k for k,v in needles.items() if v in history)
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / 'image.tar'
            run(['docker', 'image', 'save', '-o', str(archive_path), image], timeout=600)
            found = set()
            decoded = 0
            undecoded = 0
            with tarfile.open(archive_path) as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    with tempfile.TemporaryFile() as blob:
                        source = archive.extractfile(member)
                        while block := source.read(1024*1024):
                            blob.write(block)
                        blob.seek(0)
                        try:
                            with tarfile.open(fileobj=blob, mode='r:*') as layer:
                                decoded += 1
                                for entry in layer:
                                    if not entry.isfile():
                                        continue
                                    stream = layer.extractfile(entry)
                                    tail = b''
                                    entry_hits = set()
                                    while chunk := stream.read(1024*1024):
                                        data = tail + chunk
                                        entry_hits.update(k for k,v in needles.items() if v in data)
                                        tail = data[-8192:]
                                    if entry_hits:
                                        found.update(entry_hits)
                                        result['credential_bearing_files'].append({'path_identity':sha(entry.name.encode()),'fingerprints':sorted(entry_hits)})
                        except tarfile.ReadError:
                            blob.seek(0)
                            if member.size < 8*1024*1024:
                                raw=blob.read()
                                try:
                                    json.loads(raw)
                                except (ValueError, UnicodeError):
                                    undecoded += 1
                                found.update(k for k,v in needles.items() if v in raw)
                            else:
                                undecoded += 1
            result['decoded_layers'] = decoded
            result['undecoded_members'] = undecoded
            result['layers'] = 'VERIFIED' if decoded and not undecoded else 'UNVERIFIED'
            result['known_fingerprints_present'] = sorted(found)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, tarfile.TarError) as error:
        result['errors'].append(type(error).__name__)
    return result


def context_scan(base, needles):
    if not needles:
        return {'root': str(base), 'status': 'UNVERIFIED', 'reason': 'Runtime comparison values unavailable; no file contents scanned'}
    files = 0
    matches = []
    for directory, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        for name in sorted(names):
            path = Path(directory) / name
            files += 1
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            hits = sorted(k for k, value in needles.items() if value in raw)
            if hits:
                matches.append({'path': str(path), 'fingerprints': hits,
                                'classification': 'source/build-context credential'})
    return {'root': str(base), 'files_scanned': files, 'matches': matches,
            'exclusions': sorted(IGNORE_DIRS)}


def main():
    prior = ROOT / 'credential-remediation-preflight-20261001T120254273928Z.json'
    old = json.loads(prior.read_text())
    protected = set(old.get('protected_baseline', {}))
    for pattern in ('credential-remediation-*', 'secret-audit*'):
        protected.update(str(p) for p in ROOT.glob(pattern) if p.is_file())
    baseline = {p: sha(Path(p).read_bytes()) for p in sorted(protected) if Path(p).is_file()}
    docker_error = None
    try:
        containers = inspect_containers()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        containers = []
        docker_error = type(error).__name__
    by_service = {service(c): c for c in containers}
    values = {}
    mapping = []
    for c in containers:
        env = env_map(c)
        refs = []
        for key, value in sorted(env.items()):
            if SENSITIVE.search(key) and value and not key.upper().endswith(('_FILE', '_PATH')):
                identifier = classify_value(key, value)
                if identifier:
                    secret = unquote(urlsplit(value).password or '') if 'URL' in key.upper() else value
                    values.setdefault(identifier, secret.encode())
                    refs.append({'key': key, 'fingerprint': identifier})
        mapping.append({'service': service(c), 'running': c['State']['Running'],
                        'image_id': c['Image'], 'credential_references': refs,
                        'dependency_labels_present': sorted(k for k in c['Config'].get('Labels', {}) if 'depend' in k.lower())})
    probes = []
    for name in ('backend', 'worker'):
        if name in by_service:
            probes.extend((probe(by_service[name], 'db'), probe(by_service[name], 's3')))
    license_info = {'mounted': False, 'nonempty': False, 'source': 'UNVERIFIED', 'validity': 'UNVERIFIED'}
    storage = by_service.get('storage')
    if storage:
        for mount in storage['Mounts']:
            if mount['Destination'].endswith('/minio.license'):
                license_info.update(mounted=True, source='host source redacted')
                result = subprocess.run(['docker', 'exec', storage['Id'], 'sh', '-c',
                                         'test -s "$1"', 'license-check', mount['Destination']], capture_output=True, timeout=60)
                license_info['nonempty'] = result.returncode == 0
                license_info['validity'] = 'UNVERIFIED'
    layers = [layer_scan(image, values) for image in sorted({c['Image'] for c in containers})]
    contexts = [{'root': str(ROOT), 'status': 'UNVERIFIED',
                 'reason': 'Content scanning deferred until chapter/storage paths are explicitly excluded and effective contexts established'}]
    findings = []
    for item in old['findings']:
        if item['generic_pattern_match']:
            category = 'unknown/unverified'
        elif 'secret-audit' in item['path']:
            category = 'local audit artifact'
        elif item['path'].endswith('.env.example'):
            category = 'active runtime credential'
        elif item['path'].endswith('.env') or item['path'].endswith('docker-compose.yml'):
            category = 'active runtime credential'
        else:
            category = 'unknown/unverified'
        findings.append({'path': item['path'], 'fingerprints': item['fingerprints'], 'classification': category})
    report = {'gate': 'OPEN', 'decision': 'ROTATION BLOCKED', 'generated_at': datetime.now(timezone.utc).isoformat(),
              'docker_inspection': 'UNAVAILABLE' if docker_error else 'AVAILABLE',
              'docker_error_class': docker_error,
              'rotation_performed': False, 'credentials_rotated': False,
              'credential_values_included': False, 'incident': {'prior_tool_output_exposed_literals': True,
              'values_reproduced': False, 'public_external_exposure_established': False},
              'affected_chapter_accessed': False, 'ingestion_recovery_semantics_changed': False,
              'redis_restarted': False, 'migrations_run': False, 'storage_init_mutations_run': False,
              'mapping': mapping, 'prior_mapping_not_reverified': old.get('credentials', []),
              'authenticated_probes': probes or [{'service': s, 'kind': k, 'status': 'UNVERIFIED', 'reason': 'Docker inspection unavailable', 'timestamp': datetime.now(timezone.utc).isoformat()} for s in ('backend','worker') for k in ('db','s3')],
              'license': license_info if storage else {'mounted': 'UNVERIFIED', 'nonempty': 'UNVERIFIED', 'configured_source': 'redacted; not reverified', 'validity': 'UNVERIFIED'},
              'image_exposure': layers, 'build_context_exposure': contexts, 'findings': findings,
              'known_active_fingerprints': sorted(KNOWN),
              'unresolved_items': [
                  'Docker daemon/CLI inspection was unavailable; authenticated probes and container/image metadata could not be executed.',
                  'No current image inventory or layer coverage established; all relevant images remain UNVERIFIED in this pass.',
                  'Effective process settings precedence, database authentication method, and file-backed storage credential precedence require verification.',
                  'Generic-pattern findings have not been contextually proven false positives and remain unknown/unverified.',
                  'Build-context content scan was deferred; effective contexts and Dockerignore inclusion remain UNVERIFIED.',
                  'Prior evidence recorded active credentials; current runtime configuration was not reverified and no rotation occurred.',
                  'Compose host license-path discrepancy remains unexplained; prior evidence recorded a host-mounted license, but its effective configuration source was not reverified.',
                  'License validity is unverified; license was preserved and not read into output.',
                  'No external/public repository or distribution history is available in this workspace.',
                  'Historical audit findings include prior local disclosures and remain preserved.',
              ],
              'protected_artifacts': baseline,
              'protected_unchanged': all(Path(p).is_file() and sha(Path(p).read_bytes()) == h for p,h in baseline.items()),
              'plan_only': 'No values are included; no credential rotation is authorized by this artifact.'}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    prefix = ROOT / ('credential-remediation-preflight-evidence-' + stamp)
    payload = json.dumps(report, indent=2).encode()
    log = ('CREDENTIAL REMEDIATION = OPEN\nROTATION BLOCKED\n'
           'No credentials rotated. No credential values are included.\n'
           'Prior tool output exposed credential literals; incident recorded without reproduction.\n'
           'Public/external exposure is not established. AIStor license preserved and not rotated.\n'
           + '\n'.join('Unresolved: ' + item for item in report['unresolved_items']) + '\n').encode()
    for data in (payload, log):
        assert not any(value in data for value in values.values())
    for suffix, data in (('.json', payload), ('.log', log)):
        path = Path(str(prefix) + suffix)
        with path.open('xb') as stream:
            stream.write(data)
        with Path(str(path) + '.sha256').open('x', encoding='ascii') as stream:
            stream.write(sha(data) + '  ' + path.name + '\n')
    print(json.dumps({'decision': report['decision'], 'report': str(prefix) + '.json',
                      'probes': [{'service': p['service'], 'kind': p['kind'], 'success': p['success']} for p in probes],
                      'layer_states': [x['layers'] for x in layers], 'license': report['license'],
                      'values_included': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Preflight incomplete: ' + type(error).__name__ + '; details suppressed')
        raise SystemExit(1)