"""Sanitized audit: prints category-level booleans, never matching content."""
import base64
import io
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
NAME = re.compile(r'PASSWORD|SECRET|TOKEN|API_KEY|DATABASE_URL|REDIS_URL', re.IGNORECASE)
PATTERN = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{32,}')


def cmd(*args):
    r = subprocess.run(args, capture_output=True, check=False)
    if r.returncode:
        raise RuntimeError('audit command unavailable')
    return r.stdout


def report(category, failed):
    print(('FAIL' if failed else 'PASS') + ': ' + category, flush=True)


try:
    containers = json.loads(cmd('docker', 'inspect', *[
        'recap-studio-'+n+'-1' for n in ('backend','worker','frontend','storage','postgres','redis')
    ]))
except (OSError, RuntimeError, json.JSONDecodeError):
    report('Docker runtime inspection', True)
    containers = []
license_path = Path(r'C:\Users\MSC\Downloads\minio.license')
try:
    license_data = license_path.read_bytes().strip()
except OSError:
    report('license file availability '+license_path.name, True)
    license_data = b''
license_patterns = ([license_data, base64.b64encode(license_data)] if license_data else [])
license_patterns += [v.strip() for v in license_data.splitlines() if len(v.strip()) > 40]
values = set()
for c in containers:
    for item in c['Config'].get('Env', []):
        key, _, value = item.partition('=')
        if NAME.search(key) and value:
            if 'URL' in key:
                value = urlparse(value).password or ''
            if value:
                values.add(value.encode())
for path in ROOT.rglob('.env*'):
    for line in path.read_text(errors='ignore').splitlines():
        key, _, value = line.partition('=')
        if NAME.search(key) and value and not value.startswith('${'):
            if 'URL' in key:
                value = urlparse(value).password or ''
            if value:
                values.add(value.strip('"\'').encode())
patterns = license_patterns + [v for v in values if len(v) >= 6]
patterns += [base64.b64encode(v) for v in values if len(v) >= 6]


def matches(stream, license_only=False):
    needles = license_patterns if license_only else patterns
    if not needles and license_only:
        return False
    tail = b''
    found = False
    while block := stream.read(1024*1024):
        data = tail + block
        found |= any(v in data for v in needles)
        if not license_only:
            found |= bool(PATTERN.search(data))
        tail = data[-max(8192, max(map(len, needles), default=0)):]
    return found


def tar_scan(stream):
    failed = False
    with tarfile.open(fileobj=stream, mode='r|*') as archive:
        for member in archive:
            if member.isfile():
                failed |= 'minio.license' in member.name.lower()
                failed |= matches(archive.extractfile(member))
    return failed


def file_scan(path, license_only=False):
    try:
        with path.open('rb') as stream:
            return matches(stream, license_only=license_only)
    except (OSError, ValueError):
        return True


for c in containers:
    name = c['Name'].strip('/')
    logs = subprocess.run(['docker','logs',c['Id']], capture_output=True, check=False)
    report('logs '+name, matches(io.BytesIO(logs.stdout+logs.stderr)))
    # Runtime DB/S3 credentials are expected only on private service environments.
    env = json.dumps(c['Config'].get('Env',[])).encode()
    # Credentials in backend/worker/storage private runtime environments are expected;
    # this check only rejects license material in any environment and rejects any
    # credential-like material in the frontend environment.
    report('runtime environment '+name,
           matches(io.BytesIO(env), license_only=True) or
           ('frontend' in name and matches(io.BytesIO(env))))
    mounts = [m for m in c['Mounts'] if 'minio.license' in m['Destination']]
    report('Docker secret mounts '+name, bool(mounts) if 'storage' not in name
           else len(mounts)!=1 or mounts[0]['RW'])
    if any(n in name for n in ('backend','worker','frontend')):
        with tempfile.TemporaryDirectory() as directory:
            exported = Path(directory)/'container.tar'
            result = subprocess.run(
                ['docker', 'export', '-o', str(exported), c['Id']], capture_output=True, check=False
            )
            report('exact running filesystem/build output '+name,
                   result.returncode != 0 or file_scan(exported, license_only=True))

categories = {}
for path in ROOT.rglob('*'):
    if not path.is_file() or path.name in {'.env', '.env.example'}:
        continue
    category = ('private env configuration' if path.name == '.env' else
                'frontend build/source' if 'frontend' in path.parts else
                'documentation' if 'docs' in path.parts else
                'working tree/test output')
    try:
        with path.open('rb') as stream:
            # Private .env values are checked separately; source/docs/frontend/test
            # output must not contain license material or runtime credential values.
            hit = matches(stream)
            hit |= any(value in path.read_bytes() for value in values)
        hit |= 'minio.license' in path.name.lower()
        categories[category] = categories.get(category,False) or hit
    except OSError:
        categories[category] = True
for category, failed in categories.items():
    report(category, failed)
try:
    report('private rendered Compose license exposure', matches(io.BytesIO(cmd('docker','compose','config')),True))
except (OSError, RuntimeError):
    report('rendered Compose coverage', True)

for image in sorted({c['Image'] for c in containers}):
    # Exact running image ID, not a possibly moved tag.
    try:
        report('exact image history '+image[:19], matches(io.BytesIO(cmd('docker','history','--no-trunc',image)), True))
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory)/'image.tar'
            cmd('docker','image','save','-o',str(saved),image)
            failed = False
            decoded = 0
            with tarfile.open(saved) as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    stream = archive.extractfile(member)
                    if member.name.endswith('layer.tar') or '/blobs/sha256/' in '/'+member.name:
                        with tempfile.TemporaryFile() as layer:
                            while block := stream.read(1024*1024):
                                layer.write(block)
                            layer.seek(0)
                            magic = layer.read(4); layer.seek(0)
                            if magic == b'\x28\xb5\x2f\xfd':
                                raise RuntimeError('zstd layer not decoded')
                            try:
                                failed |= tar_scan(layer)
                                decoded += 1
                            except tarfile.ReadError:
                                layer.seek(0)
                                # Config/manifest blobs must be JSON; never silently skip a layer.
                                raw = layer.read()
                                json.loads(raw)
                                failed |= matches(io.BytesIO(raw), True)
                    else:
                        failed |= matches(stream, True)
            report('decoded image layers '+image[:19], failed or decoded==0)
    except (OSError, RuntimeError, tarfile.TarError, ValueError, json.JSONDecodeError):
        report('exact image/compressed-layer coverage '+image[:19],True)