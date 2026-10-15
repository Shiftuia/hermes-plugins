import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import approval_api as api
_published_ids_from_db = api._published_ids

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'dev/automation/schemas'))
from validate_package import canonical_sha256


@pytest.fixture
def site(tmp_path, monkeypatch):
    content = tmp_path / 'holyshifted/content'
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
    monkeypatch.setattr(api, '_published_ids', lambda: set())
    monkeypatch.setattr(api, 'current_writer', lambda: 'plugin')
    # shared.publishing_ledger lands on main via the windmill-recovery/2 deploy; namespace packages
    # let this worktree contribute it alongside main's shared/ until the merge lands.
    monkeypatch.syspath_prepend(str(Path.home() / 'dev/automation-wt/wr2'))
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
    assert 'already approved with this hash' not in api.assess(path)['reasons']
    package['platforms']['youtube']['title'] = 'Changed'
    path.write_text(json.dumps(package))
    item = api.assess(path)
    assert item['status'] == 'approval invalid (package changed)' and item['can_approve']
    assert client.get('/api/plugins/upload-post-analytics/upcoming').json()['videos'][0]['can_approve']


def test_per_platform_manual_route_from_package(site):
    client, package, path, _ = site
    package['platforms'].update({'telegram': {'caption': 'Hi'}, 'tiktok': {'caption': 'Clip'}})
    package['manual_steps'] = ['TikTok is not connected to Upload-Post: Dima uploads the clean export by hand on Tue 2026-10-13 around 20:00 Asia/Jerusalem.']
    path.write_text(json.dumps(package))
    url = '/api/plugins/upload-post-analytics/upcoming'
    row = client.get(url).json()['videos'][0]
    assert row['platforms'] == ['youtube', 'telegram', 'tiktok']
    assert row['manual_platforms'] == ['tiktok']
    package['manual_steps'] = []
    path.write_text(json.dumps(package))
    assert client.get(url).json()['videos'][0]['manual_platforms'] == []


@pytest.mark.parametrize('step,manual', [
    ('Instagram: pin the first comment (Upload-Post posts it, pinning is by hand).', False),
    ('Instagram: Upload-Post uploads the video; pin the comment by hand.', False),
    ('Instagram: upload the video via API then pin the comment by hand.', False),
    ('Instagram: upload the cover by hand after publishing.', False),
    ('Instagram: upload the clean export by hand.', True),
    ('Instagram: upload the video manually.', True),
    ('Instagram: manual upload required.', True),
    ('Instagram is not connected to Upload-Post.', True),
])
def test_manual_upload_not_comment_pinning(site, step, manual):
    client, package, path, _ = site
    package['platforms']['instagram'] = {'caption': 'Clip'}
    package['manual_steps'] = [step]
    path.write_text(json.dumps(package))
    row = client.get('/api/plugins/upload-post-analytics/upcoming').json()['videos'][0]
    assert ('instagram' in row['manual_platforms']) is manual


def test_june_preview_icon_and_cover(site):
    client, package, _, _ = site
    video = api.CONTENT.parent.parent / 'grandma-june/content/GJ-2026-999'
    video.mkdir(parents=True)
    (video / 'movie.mp4').write_bytes(b'media')
    (video / 'cover.jpg').write_bytes(b'cover')
    data = {**package, 'video_id': video.name, 'brand': 'grandma-june',
            'platforms': {**package['platforms'], 'tiktok': {'caption': 'June Short'}}}
    (video / 'package.json').write_text(json.dumps(data))
    response = client.get('/api/plugins/upload-post-analytics/upcoming').json()
    june = next(row for row in response['videos'] if row['id'] == video.name)
    assert 'tiktok' in june['platforms'] and 'tiktok' not in june['manual_platforms']
    assert client.get('/api/plugins/upload-post-analytics/cover/' + video.name).status_code == 200


@pytest.mark.parametrize('minutes', [120, 1, -60])
def test_approval_time_does_not_block_or_publish(site, monkeypatch, minutes):
    client, package, path, _ = site
    package['schedule']['publish_at'] = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')
    path.write_text(json.dumps(package))
    before = path.read_bytes()
    item = api.assess(path)
    assert item['can_approve'] and item['reasons'] == []
    assert path.read_bytes() == before
    listing = client.get('/api/plugins/upload-post-analytics/upcoming').json()
    row = listing['videos'][0]
    assert row['can_approve'] and row['status'] != 'published'
    if minutes < 0:
        assert row['status'] == 'missed release · approval required'
    headers = {'Origin': 'http://testserver', 'X-UPA-CSRF': listing['csrf']}
    url = '/api/plugins/upload-post-analytics/approval/' + path.parent.name
    stale = {'action': 'approve', 'sha256': '0' * 64}
    assert client.post(url, json=stale, headers=headers).status_code == 409
    (path.parent / 'cover.jpg').unlink()
    assert not api.assess(path)['can_approve']
    payload = {'action': 'approve', 'sha256': row['sha256']}
    assert client.post(url, json=payload, headers=headers).status_code == 409
    (path.parent / 'cover.jpg').write_bytes(b'cover')
    calls = []
    monkeypatch.setattr(api, '_note', lambda _id: path.parent / 'note.md')
    monkeypatch.setattr(api, '_run', lambda *args, **kwargs: calls.append(args) or '')
    audits = []
    monkeypatch.setattr(api, '_audit', audits.append)
    assert client.post(url, json=payload, headers=headers).status_code == 200
    approved = json.loads(path.read_text())
    assert approved['schedule'] == package['schedule']
    assert approved['platforms'] == package['platforms']
    assert approved['approval']['package_sha256'] == row['sha256']
    assert approved['approval']['approved_via'] == 'dashboard'
    assert datetime.fromisoformat(approved['approval']['approved_at'].replace('Z', '+00:00')) <= datetime.now(timezone.utc)
    assert any(call[:2] == ('git', 'commit') for call in calls)
    assert any(call[:2] == ('git', 'push') for call in calls)
    assert len(audits) == 1
    assert not api._has_state(path.parent.name)
    if minutes < 0:
        row = client.get('/api/plugins/upload-post-analytics/upcoming').json()['videos'][0]
        assert row['status'].startswith('missed release · approved')
        assert not row['can_approve']


@pytest.mark.parametrize('release', [None, 'not-a-date'])
def test_invalid_schedule_still_blocks_approval(site, release):
    _, package, path, _ = site
    package['schedule']['publish_at'] = release
    path.write_text(json.dumps(package))
    item = api.assess(path)
    assert not item['can_approve']
    assert 'publish_at is not set or invalid' in item['reasons']


def test_grey_reasons_and_dispatched(site):
    _, package, path, owners = site
    package['schedule']['publish_at'] = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
    path.write_text(json.dumps(package))
    assert api.assess(path)['can_approve']
    assert api.assess(path)['reasons'] == []
    owners.write_text(json.dumps({path.parent.name: 'Dima'}))
    assert api.assess(path)['status'] == 'manual · pending'
    owners.write_text('{}')
    api.STATE.mkdir()
    (api.STATE / (path.parent.name + '.json')).write_text('{"dispatch": {"state": "launched"}}')
    assert api.assess(path)['status'] == 'dispatched'
    assert not api.assess(path)['can_approve']
    (path.parent / 'cover.jpg').unlink()
    assert 'thumbnail' in ' '.join(api.assess(path)['reasons'])


def test_manual_approved_planned_and_no_mutation(site):
    client, package, path, owners = site
    owners.write_text(json.dumps({path.parent.name: {'owner': 'manual sheet',
                                                    'planned': {'date': '2026-10-14', 'note': 'evening, by hand'}}}))
    package['schedule']['publish_at'] = None
    digest = canonical_sha256(package)
    package['approval'] = {'approved_at': '2026-10-11T08:04:54Z', 'package_sha256': digest}
    path.write_text(json.dumps(package))
    item = api.assess(path)
    assert item['status'] == 'manual · approved ' + digest[:8]
    assert item['reasons'] == []
    assert not item['can_approve'] and not item['can_unapprove']
    listing = client.get('/api/plugins/upload-post-analytics/upcoming').json()
    row = listing['videos'][0]
    assert row['planned'] == {'date': '2026-10-14', 'note': 'evening, by hand'}
    assert row['manual'] and row['status'] == item['status']
    assert row['manual_platforms'] == ['youtube']
    url = '/api/plugins/upload-post-analytics/approval/' + path.parent.name
    headers = {'Origin': 'http://testserver', 'X-UPA-CSRF': listing['csrf']}
    for action in ('approve', 'unapprove'):
        assert client.post(url, json={'action': action, 'sha256': digest}, headers=headers).status_code == 409
    assert json.loads(path.read_text()) == package
    api.STATE.mkdir(exist_ok=True)
    (api.STATE / (path.parent.name + '.json')).write_text('{"live_urls": {"youtube": "https://youtube.com/watch?v=1"}}')
    assert client.get('/api/plugins/upload-post-analytics/upcoming').json()['videos'] == []


def test_manual_leaves_upcoming_only_when_analytics_published(site, tmp_path, monkeypatch):
    client, package, path, owners = site
    owners.write_text(json.dumps({path.parent.name: {'owner': 'manual', 'planned': {'date': '2026-10-14', 'note': 'evening'}}}))
    package['schedule']['publish_at'] = None
    path.write_text(json.dumps(package))
    database = tmp_path / 'analytics.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE posts (content_id TEXT, published_at INTEGER, platform TEXT, source TEXT, gone_at INTEGER, duplicate_of INTEGER)')
        db.execute('INSERT INTO posts VALUES (?,?,?,?,?,?)', (path.parent.name, None, 'youtube', 'media', None, None))
    monkeypatch.setattr(api, 'DB_PATH', database)
    monkeypatch.setattr(api, '_published_ids', _published_ids_from_db)
    url = '/api/plugins/upload-post-analytics/upcoming'
    assert len(client.get(url).json()['videos']) == 1
    with sqlite3.connect(database) as db:
        db.execute('UPDATE posts SET published_at=?', (int(datetime.now(timezone.utc).timestamp()) + 3600,))
    assert len(client.get(url).json()['videos']) == 1
    with sqlite3.connect(database) as db:
        db.execute('UPDATE posts SET published_at=?', (int(datetime.now(timezone.utc).timestamp()) - 60,))
    assert client.get(url).json()['videos'] == []


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
    ledger_state = json.loads((api.STATE / (path.parent.name + '.json')).read_text())
    fact = ledger_state['_recovery']['facts'][-1]
    assert (fact['principal'], fact['assurance'], fact['channel'], fact['status']) == (
        'dashboard-session', 'csrf-session', 'plugin', 'approved')
    assert fact['principal'] != 'Dima'
    payload['action'] = 'unapprove'
    dashboard_state = api.STATE / (path.parent.name + '.json')
    dashboard_state.write_text(json.dumps({**ledger_state, 'dispatch': {'state': 'launched'}}))
    assert client.post(url, json=payload, headers=headers).status_code == 409
    dashboard_state.write_text(json.dumps(ledger_state))
    assert client.post(url, json=payload, headers=headers).status_code == 200
    assert json.loads(path.read_text())['approval'] is None
    assert client.post(url, json=payload, headers=headers).json()['unchanged']
    revoked = json.loads(dashboard_state.read_text())['_recovery']['facts'][-1]
    assert revoked['status'] == 'revoked' and revoked['principal'] == 'dashboard-session'


@pytest.mark.parametrize('writer', ['hq', None])
def test_switch_away_from_plugin_blocks_writes_and_hides_buttons(site, monkeypatch, writer):
    client, package, path, _ = site
    monkeypatch.setattr(api, 'current_writer', lambda: writer)
    listing = client.get('/api/plugins/upload-post-analytics/upcoming').json()
    row = listing['videos'][0]
    assert not row['can_approve'] and not row['can_unapprove'] and api.MOVED in row['reasons']
    calls = []
    monkeypatch.setattr(api, '_run', lambda *args, **kwargs: calls.append(args) or '')
    url = '/api/plugins/upload-post-analytics/approval/' + path.parent.name
    headers = {'Origin': 'http://testserver', 'X-UPA-CSRF': listing['csrf']}
    for action in ('approve', 'unapprove'):
        r = client.post(url, json={'action': action, 'sha256': row['sha256']}, headers=headers)
        assert r.status_code == 409 and r.json()['detail'] == api.MOVED
    assert json.loads(path.read_text()) == package and calls == []


def test_writer_file_parsing(tmp_path):
    from approval_writer import current_writer
    f = tmp_path / 'writer'
    assert current_writer(f) == 'plugin'  # no file: the pre-HQ state
    for text, expected in (('hq\n', 'hq'), ('plugin', 'plugin'), ('HQ', None), ('', None), ('both', None)):
        f.write_text(text)
        assert current_writer(f) == expected
