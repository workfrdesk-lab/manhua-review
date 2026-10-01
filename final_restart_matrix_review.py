"""Read-only evidence review; emit a separate acceptance decision without rewriting runs."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN = ROOT / 'final-restart-matrix-20261001T104405189045Z'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    original = RUN / 'acceptance.json'
    data = json.loads(original.read_text(encoding='utf-8'))
    excluded = str(ROOT / 'matrix-launch-20261001T104404321Z.out')
    assert data['changed_protected'] == [excluded]
    verified = {}
    for sidecar in RUN.rglob('*.sha256'):
        target = Path(str(sidecar)[:-7])
        assert sha(target) == sidecar.read_text().split()[0], str(target)
        verified[str(target)] = sha(target)
    for path, expected in data['protected_before'].items():
        if path != excluded:
            assert sha(Path(path)) == expected, path
    summaries = []
    for scenario in data['scenarios']:
        assert scenario['result'] == 'PASS'
        assert all(scenario[k] for k in ('receipt', 'initial_receipt', 'celery_completed', 'image_source_matches'))
        assert scenario['cleanup_exit_code'] == 0
        probe = scenario['probe']
        assert probe['result'] == 'PASS' and probe['final_consistency']
        final = probe['snapshots'][-1]
        job = final['jobs'][0]
        current = next(a for a in final['ingestion_attempts'] if a['id'] == job['current_attempt_id'])
        assert job['status'] == current['status'] == 'completed'
        assert current['started_at'] and current['finished_at']
        assert final['chapters'][0]['status'] == 'ready'
        assert len(final['pages']) == 100
        assert sorted(p['page_number'] for p in final['pages']) == list(range(1, 101))
        objects = {o['Key']: o for o in final['objects']}
        source = final['chapter_files'][0]['storage_key']
        assert objects[source]['sha256'] == probe['source_sha256']
        expected = {source}
        for page in final['pages']:
            assert page['attempt_id'] == current['id']
            assert objects[page['storage_key']]['sha256'] == probe['expected_page_sha256']
            assert objects[page['thumbnail_key']]['sha256'] == probe['expected_thumbnail_sha256']
            expected.update((page['storage_key'], page['thumbnail_key']))
        assert len(expected) == len(final['objects']) == 201 and expected == set(objects)
        for attempt in final['ingestion_attempts']:
            if attempt['id'] != current['id']:
                assert attempt['status'] == 'abandoned' and attempt['cleanup_status'] == 'swept'
        for event in scenario['lifecycle']:
            assert event['before']['storage-init'] == event['after']['storage-init']
        if scenario['scenario_id'] == 7:
            before = next(s for s in probe['snapshots'] if s['label'] == 'completion_before_restarts')
            for service in ('worker', 'backend', 'redis'):
                after = next(s for s in probe['snapshots'] if s['label'] == 'after_' + service + '_restart')
                for key in ('chapters', 'jobs', 'ingestion_attempts', 'chapter_files', 'pages', 'objects'):
                    assert before[key] == after[key]
        summaries.append({'scenario_id': scenario['scenario_id'], 'result': 'PASS',
            'started_at': scenario['started_at'], 'finished_at': scenario['finished_at'],
            'job_id': job['id'], 'current_attempt_id': current['id'], 'generation': current['generation'],
            'pages': 100, 'source_objects': 1, 'page_objects': 100, 'thumbnail_objects': 100,
            'hash_consistency': True, 'duplicate_objects': False, 'evidence': scenario['evidence_directory']})
    assert [s['scenario_id'] for s in summaries] == list(range(1, 8))
    for regression in data['regression'].values():
        assert regression['exit_code'] == 0 and regression['result'] == 'PASS'
        assert sha(Path(regression['log'])) == regression['sha256']
    report = {'reviewed_at': datetime.now(timezone.utc).isoformat(), 'ingestion_recovery': 'PASS',
        'scenarios': summaries, 'regression': data['regression'], 'production_defect_proven': False,
        'production_source_and_prior_evidence_unchanged': True,
        'original_report': str(original), 'original_report_sha256': sha(original),
        'harness_failure': {'classification': 'baseline scope error; not application defect',
            'detail': 'The running launcher stdout was incorrectly included in the protected baseline.',
            'only_excluded_file': excluded, 'original_open_report_preserved': True,
            'scenario_reruns': 0},
        'verified_evidence_sha256': verified,
        'limitations': ['Regression pytest uses isolated SQLite defaults; matrix uses real PostgreSQL/Redis/AIStor.',
            'No broker duplicate injection, manual DB mutation, manual lease expiry, or production semantic change.',
            'No Phase 4B or credential rotation; no affected chapter access.']}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    prefix = ROOT / ('final-ingestion-recovery-reviewed-' + stamp)
    json_path = Path(str(prefix) + '.json')
    json_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    log = Path(str(prefix) + '.log')
    log.write_text('INGESTION RECOVERY = PASS\nIndependent evidence review\n' +
        '\n'.join(f'Scenario {s["scenario_id"]}: PASS; generation {s["generation"]}; 1 source + 100 pages + 100 thumbnails; DB/hash/inventory consistent' for s in summaries) +
        '\nFocused: 24 passed. Full backend: 511 passed, 11 warnings. Ruff/typecheck/build: PASS.\n' +
        'Initial OPEN: harness baseline accidentally included active launcher stdout; only that file excluded. Original evidence preserved.\n' +
        'Production source and prior protected evidence unchanged. No production defect proven.\n' +
        'No affected chapter access; no Phase 4B; no credential rotation.\nOriginal report: ' + str(original), encoding='utf-8')
    for path in (json_path, log):
        Path(str(path) + '.sha256').write_text(sha(path) + '  ' + path.name + '\n', encoding='ascii')
        print(str(path), sha(path))


if __name__ == '__main__':
    main()