import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import approval_api as api

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'dev/automation/schemas'))
from validate_package import canonical_sha256


@pytest.fixture
def site(tmp_path, monkeypatch):
    content = tmp_path / 'content'
    video = content / 'HS-2026-999'
    video.mkdir(parents=True)
    (video / 'movie.mp4').write_bytes(b'media')
    (video / 'cover.jpg').write_bytes(b'cover')
    path = video / 'package.json'
    package = {'video_id': video.name, 'schedule': {'publish_at': (datetime.now(timezone.utc) + timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ')},
               'platforms': {'youtube': {'title': 'Test', 'description': 'First line\nSecond', 'first_comment': 'Yes'}},
               'files': {'video': 'movie.mp4', 'thumbnail': 'cover.jpg'}, 'approval': None}
    path.write_text(json.dumps(package))
    owners = tmp_path / 'owners.json'
    owners.write_text('{}')
    monkeypatch.setattr(api, 'CONTENT', content)
    monkeypatch.setattr(api, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(api, 'OWNERS', owners)
    def check(path, approval=False):
        data = json.loads(path.read_text())
        sha = canonical_sha256(data)
        errors = []
        if data.get('approval') and data['approval']['package_sha256'] != sha:
            errors.append('approval hash mismatch: computed ' + sha)
        if approval and not data.get('approval'):
            errors.append('approval missing')
        if not (path.parent / data['files']['video']).is_file():
            errors.append('file missing: video')
        if not (path.parent / data['files']['thumbnail']).is_file():
            errors.append('file missing: thumbnail')
        return {'sha256': sha, 'errors': errors, 'warnings': []}
    monkeypatch.setattr(api, 'validate', check)
    app = FastAPI()
    app.include_router(api.router, prefix='/api/plugins/upload-post-analytics')
    client = TestClient(app)
    return client, package, path, owners


def test_ready_draft_approved_changed(site):
    client, package, path, _ = site
    item = api.assess(path)
    assert item['status'] == 'ready to approve' and item['can_approve']
    package['approval'] = {'approved_at': datetime.now(timezone.utc).isoformat(), 'approved_via': 'dashboard',
                           'package_sha256': item['digest']}
    path.write_text(json.dumps(package))
    assert api.assess(path)['status'].startswith('approved sha')
    assert not api.assess(path)['can_approve']
    package['platforms']['youtube']['title'] = 'Changed'
    path.write_text(json.dumps(package))
    item = api.assess(path)
    assert item['status'] == 'approval invalid (package changed)' and item['can_approve']
    assert client.get('/api/plugins/upload-post-analytics/upcoming').json()['videos'][0]['can_approve']


def test_grey_reasons_and_dispatched(site):
    _, package, path, owners = site
    package['schedule']['publish_at'] = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
    path.write_text(json.dumps(package))
    assert '3 hours' in ' '.join(api.assess(path)['reasons'])
    owners.write_text(json.dumps({path.parent.name: 'Dima'}))
    assert api.assess(path)['status'] == 'manual owner'
    owners.write_text('{}')
    api.STATE.mkdir()
    (api.STATE / (path.parent.name + '.json')).write_text('{"dispatch": {"state": "launched"}}')
    assert api.assess(path)['status'] == 'dispatched'
    assert not api.assess(path)['can_approve']
    (path.parent / 'cover.jpg').unlink()
    assert 'thumbnail' in ' '.join(api.assess(path)['reasons'])


def test_endpoint_csrf_origin_stale_manual_late_and_write(site, monkeypatch):
    client, package, path, owners = site
    url = '/api/plugins/upload-post-analytics/approval/' + path.parent.name
    listing = client.get('/api/plugins/upload-post-analytics/upcoming').json()
    sha = listing['videos'][0]['sha256']
    payload = {'action': 'approve', 'sha256': sha}
    headers = {'Origin': 'http://testserver', 'X-UPA-CSRF': listing['csrf']}
    assert client.get(url, headers=headers).status_code == 405
    assert client.post(url, json=payload, headers={'Origin': 'http://testserver'}).status_code == 403
    assert client.post(url, json=payload, headers={'Origin': 'http://testserver', 'X-UPA-CSRF': 'forged'}).status_code == 403
    assert client.post(url, json=payload, headers={'Origin': 'http://evil', 'X-UPA-CSRF': listing['csrf']}).status_code == 403
    assert client.post(url, json=payload, headers=headers).status_code == 500  # missing vault note, no mutation
    assert json.loads(path.read_text())['approval'] is None
    owners.write_text(json.dumps({path.parent.name: 'Dima'}))
    assert client.post(url, json=payload, headers=headers).status_code == 409
    owners.write_text('{}')
    package['schedule']['publish_at'] = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
    path.write_text(json.dumps(package))
    assert client.post(url, json=payload, headers=headers).status_code == 409
    package['schedule']['publish_at'] = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ')
    package['platforms']['youtube']['title'] = 'Changed'
    path.write_text(json.dumps(package))
    assert client.post(url, json=payload, headers=headers).status_code == 409
    fresh = api.assess(path)['digest']
    payload['sha256'] = fresh
    monkeypatch.setattr(api, '_note', lambda _id: path.parent / 'note.md')
    monkeypatch.setattr(api, '_run', lambda *args, **kwargs: '')
    monkeypatch.setattr(api, '_audit', lambda message: None)
    assert client.post(url, json=payload, headers=headers).status_code == 200
    approved = json.loads(path.read_text())['approval']
    assert approved['approved_via'] == 'dashboard' and approved['package_sha256'] == fresh
    assert client.post(url, json=payload, headers=headers).json()['unchanged']
    payload['action'] = 'unapprove'
    api.STATE.mkdir(exist_ok=True)
    (api.STATE / (path.parent.name + '.json')).write_text('{"dispatch": {"state": "launched"}}')
    assert client.post(url, json=payload, headers=headers).status_code == 409
    (api.STATE / (path.parent.name + '.json')).unlink()
    assert client.post(url, json=payload, headers=headers).status_code == 200
    assert json.loads(path.read_text())['approval'] is None
    assert client.post(url, json=payload, headers=headers).json()['unchanged']
