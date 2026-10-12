"""Dashboard approval gate for publishing packages."""
from __future__ import annotations

import fcntl
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

router = APIRouter()
CONTENT = Path.home() / 'second-brain-data/holyshifted/content'
STATE = Path.home() / 'second-brain-data/.automation/publishing'
VAULT = Path.home() / 'second-brain'
AUTOMATION = Path.home() / 'dev/automation'
PYTHON = AUTOMATION / '.venv/bin/python'
VALIDATOR = AUTOMATION / 'schemas/validate_package.py'
RENDERER = AUTOMATION / 'schemas/render_package.py'
OWNERS = AUTOMATION / 'f/publishing/manual_owners.json'
DB_PATH = Path.home() / 'dev/services/_scripts/upload-post-analytics/data/upload_post_analytics.db'
ALERT_LIB = AUTOMATION / 'host/lib'
SECRET = secrets.token_bytes(32)
VIDEO_ID = re.compile(r'(HS|GJ)-\d{4}-\d{3}\Z')


def content_root(video_id):
    match = VIDEO_ID.fullmatch(video_id)
    if not match:
        raise HTTPException(404, 'unknown video')
    return CONTENT if match[1] == 'HS' else CONTENT.parent.parent / 'grandma-june/content'


def package_path(video_id):
    content = content_root(video_id)
    path = content / video_id / 'package.json'
    if not path.is_file() or path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(content.resolve()):
        raise HTTPException(404, 'unknown video')
    return path


def validate(path, approval=False):
    command = [str(PYTHON), str(VALIDATOR), str(path), '--json']
    if approval:
        command.append('--require-approval')
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=90)
        result = json.loads(run.stdout)
        if run.returncode not in (0, 1, 2) or not isinstance(result.get('errors'), list):
            raise ValueError('validator returned an invalid result')
        return result
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {'errors': [f'validator unavailable: {exc}'], 'sha256': None, 'warnings': []}


def _state(video_id):
    path = STATE / (video_id + '.json')
    return json.loads(path.read_text()) if path.exists() else {}


def _has_state(video_id):
    return (STATE / (video_id + '.json')).exists()


def _package(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _release(data):
    raw = (data.get('schedule') or {}).get('publish_at')
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')) if raw else None
    except (ValueError, AttributeError):
        return None


def assess(path, now=None):
    now = now or datetime.now(timezone.utc)
    data = _package(path)
    check = validate(path)
    digest = check.get('sha256')
    approval = data.get('approval') or {}
    current = bool(digest and approval.get('package_sha256') == digest)
    changed = bool(approval and not current)
    reasons = [r for r in check['errors'] if not (changed and r.startswith('approval hash mismatch:'))]
    owner = json.loads(OWNERS.read_text(encoding='utf-8')).get(path.parent.name)
    planned = owner.get('planned') if isinstance(owner, dict) else None
    release = _release(data)
    if not owner:
        if not release:
            reasons.append('publish_at is not set or invalid')
        elif release < now + timedelta(hours=3):
            reasons.append('publish_at is less than 3 hours away; dispatcher window would be missed')
    state = _state(path.parent.name)
    has_state = _has_state(path.parent.name)
    if has_state:
        reasons.append('publish state exists; approval is locked after dispatch')
    files = data.get('files') or {}
    for key in ('video', 'thumbnail'):
        name = files.get(key)
        target = (path.parent / name).resolve() if isinstance(name, str) else None
        if target is None or not target.is_relative_to(path.parent.resolve()) or not target.is_file():
            reason = f'{key} file missing or outside package directory'
            if not any(reason in r or f'{key}=' in r for r in reasons):
                reasons.append(reason)
    status = ('manual · approved ' + digest[:8] if owner and current and digest else
              'manual · approval invalid' if owner and changed else
              'manual · pending' if owner else
              'published' if state.get('live_urls') or (release and release <= now) else
              'dispatched' if has_state else
              'approval invalid (package changed)' if changed else
              'approved sha ' + digest[:12] if current else
              'ready to approve' if not reasons else 'draft')
    return {'data': data, 'digest': digest, 'release': release, 'approval': approval,
            'planned': planned, 'manual': bool(owner), 'status': status, 'reasons': reasons,
            'can_approve': not owner and not current and not reasons,
            'can_unapprove': not owner and bool(approval) and not has_state}


def _manual_platforms(data):
    steps = data.get('manual_steps') or []
    manual_upload = (r'(?:not connected to Upload-Post|manual upload|\bupload(?:s|ed|ing)?\b(?!-)'
                     r'(?:(?!\b(?:pin\w*|comment\w*|cover|thumbnail)\b)[^.\n;()])*(?:by hand|manually))')
    return [name for name in (data.get('platforms') or {})
            if any(re.search(rf'\b{re.escape(name)}\b[^.\n]*{manual_upload}', step, re.IGNORECASE)
                   for step in steps if isinstance(step, str))]


def _preview(data):
    platforms = data.get('platforms') or {}
    youtube = platforms.get('youtube') or {}
    first = lambda text: (text or '').splitlines()[:3]
    return {'youtube_title': youtube.get('title'), 'description': first(youtube.get('description')),
            'first_comment': youtube.get('first_comment'),
            'platform_lines': {name: first(value.get('post') or value.get('caption') or
                                           (value.get('posts') or [''])[0])[:1]
                               for name, value in platforms.items() if isinstance(value, dict)}}


def _token():
    nonce = secrets.token_hex(24)
    return nonce + '.' + hmac.new(SECRET, nonce.encode(), 'sha256').hexdigest()


def _valid_token(token):
    if not token or not isinstance(token, str) or '.' not in token:
        return False
    nonce, signature = token.split('.', 1)
    return len(nonce) == 48 and hmac.compare_digest(signature, hmac.new(SECRET, nonce.encode(), 'sha256').hexdigest())


def _same_origin(request):
    origin = request.headers.get('origin', '')
    host = request.headers.get('host', '')
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in ('http', 'https') and parsed.netloc == host and parsed.path == '' and not parsed.query and not parsed.fragment
    except ValueError:
        return False


def _check_request(request):
    token = request.headers.get('x-upa-csrf', '')
    cookie = request.cookies.get('upa_csrf', '')
    if not _same_origin(request) or not _valid_token(token) or not hmac.compare_digest(token, cookie):
        raise HTTPException(403, 'same-origin CSRF token required')


def _published_ids():
    if not DB_PATH.exists():
        return set()
    with closing(sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True, timeout=5)) as db:
        return {row[0] for row in db.execute(
            "SELECT DISTINCT content_id FROM posts WHERE content_id IS NOT NULL AND published_at <= ? "
            "AND gone_at IS NULL AND duplicate_of IS NULL "
            "AND (platform != 'youtube' OR source IN ('media', 'vault'))",
            (int(datetime.now(timezone.utc).timestamp()),))}


@router.get('/upcoming')
def upcoming(response: Response):
    token = _token()
    response.set_cookie('upa_csrf', token, httponly=True, samesite='strict', path='/api/plugins/upload-post-analytics')
    items = []
    published = _published_ids()
    for path in sorted(p for content in (CONTENT, CONTENT.parent.parent / 'grandma-june/content')
                       for p in content.glob('??-????-???/package.json')):
        if path.is_symlink() or path.parent.is_symlink():
            continue
        try:
            item = assess(path)
            release = item['release']
            if path.parent.name in published or item['status'] == 'published' or (release and release <= datetime.now(timezone.utc)) or _state(path.parent.name).get('live_urls'):
                continue
            data = item['data']
            items.append({'id': path.parent.name, 'title': (data.get('platforms') or {}).get('youtube', {}).get('title'),
                          'publish_at': release.isoformat() if release else None, 'planned': item['planned'],
                          'manual': item['manual'],
                          'platforms': list((data.get('platforms') or {}).keys()),
                          'manual_platforms': (list((data.get('platforms') or {}).keys()) if item['manual']
                                               else _manual_platforms(data)),
                          'status': item['status'], 'sha256': item['digest'], 'reasons': item['reasons'],
                          'can_approve': item['can_approve'], 'can_unapprove': item['can_unapprove'],
                          'preview': _preview(data), 'cover': f'/api/plugins/upload-post-analytics/cover/{path.parent.name}'})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            items.append({'id': path.parent.name, 'status': 'draft', 'reasons': [str(exc)], 'can_approve': False,
                          'can_unapprove': False, 'platforms': [], 'preview': {}})
    return {'status': 'ok', 'csrf': token, 'videos': items}


@router.get('/cover/{video_id}')
def cover(video_id: str):
    path = package_path(video_id)
    name = (_package(path).get('files') or {}).get('thumbnail')
    target = (path.parent / name).resolve() if isinstance(name, str) else None
    if not target or not target.is_relative_to(path.parent.resolve()) or not target.is_file() or target.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        raise HTTPException(404, 'cover unavailable')
    return FileResponse(target, headers={'Cache-Control': 'no-store'})


def _run(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:500] or f'{args[0]} exited {result.returncode}')
    return result.stdout.strip()


def _note(video_id):
    brand = 'holyshifted' if video_id.startswith('HS-') else 'grandma-june'
    matches = list((VAULT / 'areas' / brand / 'content').glob(video_id + '-*.md'))
    if len(matches) != 1:
        raise RuntimeError(f'expected one vault note for {video_id}, found {len(matches)}')
    return matches[0]


def _audit(message):
    sys.path.insert(0, str(ALERT_LIB))
    from alert import load_discord_webhook, send_discord
    webhook, thread = load_discord_webhook()
    if not send_discord(webhook, message, thread_id=thread):
        raise RuntimeError('Discord audit delivery failed; approval is saved, inspect alerts before retry')


def _finish(video_id, action, digest, release, approval_key):
    _run('git', 'push', cwd=VAULT)
    marker = STATE / (video_id + '.dashboard-audit.json')
    event = {'action': action, 'sha256': digest, 'approval_key': approval_key}
    if marker.exists() and json.loads(marker.read_text()) == event:
        return
    _audit(f'{video_id} {"approved" if action == "approve" else "unapproved"} from dashboard, sha {digest}, publishes {release.isoformat() if release else "unset"}')
    marker.write_text(json.dumps(event) + '\n')


@router.post('/approval/{video_id}')
async def approval(video_id: str, request: Request):
    _check_request(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, 'invalid JSON')
    if not isinstance(body, dict) or body.get('action') not in ('approve', 'unapprove') or not isinstance(body.get('sha256'), str):
        raise HTTPException(400, 'action and sha256 required')
    path = package_path(video_id)
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        with (STATE / (video_id + '.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            item = assess(path)
            if item['manual']:
                raise HTTPException(409, 'manual owner; dashboard approval is unavailable')
            digest = item['digest']
            if digest is None or not hmac.compare_digest(digest, body['sha256']):
                raise HTTPException(409, 'package hash changed; refresh before approving')
            if body['action'] == 'approve':
                if item['approval'].get('package_sha256') == digest:
                    if item['approval'].get('approved_via') != 'dashboard':
                        raise HTTPException(409, 'already approved outside the dashboard')
                    _finish(video_id, 'approve', digest, item['release'], item['approval'].get('approved_at'))
                    return {'status': 'ok', 'unchanged': True}
                if not item['can_approve']:
                    raise HTTPException(409, '; '.join(item['reasons']))
            elif not item['approval'] and not _has_state(video_id):
                marker = STATE / (video_id + '.dashboard-audit.json')
                if marker.exists():
                    previous = json.loads(marker.read_text())
                    if previous.get('sha256') == digest:
                        if previous.get('action') == 'approve':
                            _finish(video_id, 'unapprove', digest, item['release'], previous.get('approval_key'))
                        return {'status': 'ok', 'unchanged': True}
                raise HTTPException(409, 'no approval to remove')
            elif not item['can_unapprove']:
                raise HTTPException(409, 'no approval to remove or publish state exists')
            approval_key = item['approval'].get('approved_at') if body['action'] == 'unapprove' else None
            note = _note(video_id)
            _run('git', 'diff', '--quiet', 'HEAD', '--', str(note), cwd=VAULT)
            data = item['data']
            data['approval'] = ({'approved_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                                 'approved_via': 'dashboard', 'package_sha256': digest}
                                if body['action'] == 'approve' else None)
            original = path.read_bytes()
            tmp = path.with_suffix('.json.tmp')
            try:
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                os.replace(tmp, path)
                if body['action'] == 'approve':
                    checked = validate(path, approval=True)
                    if checked['errors'] or checked['sha256'] != digest:
                        raise RuntimeError('approval validation failed: ' + '; '.join(checked['errors']))
                _run(str(PYTHON), str(RENDERER), str(path), str(note))
            except Exception:
                path.write_bytes(original)
                tmp.unlink(missing_ok=True)
                raise
            _run('git', 'add', '--', str(note), cwd=VAULT)
            _run('git', 'commit', '--only', '-m', f'{video_id}: record dashboard {body["action"]}', '--', str(note), cwd=VAULT)
            _finish(video_id, body['action'], digest, item['release'], data['approval']['approved_at'] if data['approval'] else approval_key)
            return {'status': 'ok', 'sha256': digest}
    except HTTPException:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(500, str(exc))
