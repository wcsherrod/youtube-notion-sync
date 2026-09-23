"""Private YouTube playlist → Notion sync. Python 3.11+."""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import threading
import sqlite3
import uuid
from google.auth.exceptions import TransportError
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

class Progress:
    """Heartbeat reports activity, not a guarantee that a request is advancing."""
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.stage = "Starting"
        self.changed = time.monotonic()
        self.started = self.changed

    def emit(self, message):
        elapsed = int(time.monotonic() - self.started)
        print(f"[{elapsed // 3600:02d}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}] {message}",
              flush=True)

    def log(self, message):
        with self.lock:
            self.stage = message
            self.changed = time.monotonic()
            self.emit(message)

    def heartbeat(self):
        while not self.stop.wait(20):
            with self.lock:
                age = int(time.monotonic() - self.changed)
                self.emit(f"Heartbeat: process running; {age}s since last progress - {self.stage}")

    def __enter__(self):
        self.stop.clear()
        self.started = self.changed = time.monotonic()
        self.worker = threading.Thread(target=self.heartbeat, daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.worker.join(timeout=1)


progress = Progress()


VERSION = '2025-09-03'
MARKER = 'YouTube importer content (managed)'
OPEN_VIDEO_FORMULA = 'if(empty(prop("Video URL")), "", link(style("↗ VIEW IN BROWSER ↗", "b", "blue"), prop("Video URL")))'
SCHEMA = {'Name': {'title': {}}, **{k: {'rich_text': {}} for k in
    ['Playlist', 'Playlist ID', 'Item ID', 'Video ID', 'Channel', 'Description',
     'Content hash', 'Transcript status', 'Transcript language']},
    **{k: {'url': {}} for k in ['Video URL', 'Playlist URL', 'Thumbnail']},
    **{k: {'date': {}} for k in ['Published', 'Added', 'Transcript checked']},
    'Position': {'number': {}}, 'In playlist': {'checkbox': {}},
    'OPEN VIDEO': {'formula': {'expression': OPEN_VIDEO_FORMULA}}}


def rt(text):
    # Bound UTF-16 units too: astral characters (including many emoji) use two.
    # Preserve every character rather than truncating descriptions/transcripts.
    chunks, start, units = [], 0, 0
    for i, char in enumerate(text):
        width = 2 if ord(char) > 0xffff else 1
        if units + width > 1800:
            chunks.append(text[start:i])
            start, units = i, 0
        units += width
    if start < len(text):
        chunks.append(text[start:])
    return [{'type': 'text', 'text': {'content': chunk}} for chunk in chunks]


def rich(text):
    return {'rich_text': rt(text)}


def plain(page, name):
    p = page.get('properties', {}).get(name, {})
    return ''.join(x.get('plain_text', x.get('text', {}).get('content', ''))
                   for x in p.get('rich_text', p.get('title', [])))


def date(value):
    return {'date': {'start': value} if value else None}


class SyncError(RuntimeError):
    """Importer-authored message safe to display without API bodies or credentials."""


class PlaylistUnavailable(SyncError):
    """Playlist disappeared or became inaccessible; preserve its records."""


class TemporaryAPIError(SyncError):
    """Network failure, rate limit, or server failure; safe to defer."""


class NotionValidationError(SyncError):
    """A rejected payload; only the per-video write boundary may defer it."""


class PlaylistDatabaseConflict(SyncError):
    """Ambiguous destination; leave this playlist and all its records untouched."""


ENV_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def load_local_environment(project_dir=None):
    """Load local credentials relative to this script without overriding CI."""
    project_dir = Path(project_dir or Path(__file__).resolve().parent).resolve()
    os.chdir(project_dir)
    env_file = project_dir / '.env'
    if env_file.is_file():
        for line_number, raw_line in enumerate(
                env_file.read_text(encoding='utf-8-sig').splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            if '=' not in line:
                raise SyncError(
                    f'Malformed .env line {line_number}; expected KEY=VALUE.')
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if not ENV_KEY.fullmatch(key):
                raise SyncError(f'Invalid .env key on line {line_number}.')
            if value.startswith(('"', "'")):
                quote = value[0]
                if len(value) < 2 or not value.endswith(quote):
                    raise SyncError(
                        f'Unterminated quoted .env value on line {line_number}.')
                value = value[1:-1]
            os.environ.setdefault(key, value)
    token_file = project_dir / 'youtube-token.json'
    if 'YOUTUBE_TOKEN_JSON' not in os.environ and token_file.is_file():
        os.environ['YOUTUBE_TOKEN_JSON'] = token_file.read_text(encoding='utf-8-sig')
    return project_dir


def validation_message(response, headers):
    try:
        body = response.json()
    except (ValueError, TypeError):
        body = {}
    message = body.get('message', 'No validation message supplied') if isinstance(body, dict) else 'No validation message supplied'
    if not isinstance(message, str):
        message = 'No validation message supplied'
    # Validation messages can echo submitted values. Strip known credentials,
    # but retain the field path, limits and offending value needed for repair.
    secrets = [headers.get('Authorization', ''), os.environ.get('NOTION_TOKEN', '')]
    auth = headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        secrets.append(auth[7:])
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        message = message.replace(secret, '[REDACTED]')
    return ''.join(c if c.isprintable() else ' ' for c in message)


def preferred_copy(copies):
    """Choose deterministically; never delete or merge the other copies."""
    def rank(page):
        status = plain(page, 'Transcript status')
        return (
            {'Full': 2, 'Partial': 1}.get(status, 0),
            bool(plain(page, 'Content hash')),
            bool(plain(page, 'Transcript status')),
            page.get('id', ''),
        )
    return max(copies, key=rank)


def index_existing(rows):
    grouped = {}
    for page in rows:
        key = plain(page, 'Item ID')
        if key:
            grouped.setdefault(key, []).append(page)
    extra = sum(len(copies) - 1 for copies in grouped.values())
    if extra:
        progress.log(f'Found {extra} extra duplicate rows; using the most complete copy of each item. Other copies are preserved.')
    return {key: preferred_copy(copies) for key, copies in grouped.items()}, extra


def error_message(exc):
    if isinstance(exc, SyncError):
        return str(exc)
    if isinstance(exc, KeyError):
        key = exc.args[0] if exc.args else ''
        if key in ('NOTION_TOKEN', 'YOUTUBE_TOKEN_JSON'):
            return f'Missing {key}; load your local environment settings before running.'
        return 'A required configuration or API response field is missing.'
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return 'Network connection failed outside the API retry handler.'
    if isinstance(exc, json.JSONDecodeError):
        return 'Invalid JSON in the configuration, OAuth token, or API response.'
    if isinstance(exc, FileNotFoundError):
        return 'A required local file is missing; check config.json and your OAuth files.'
    return f'Unexpected {type(exc).__name__}; no credentials or API response body were logged.'


def api_error_detail(response):
    """Only report documented machine-readable codes, never response messages."""
    allowed = {
        'quotaExceeded', 'dailyLimitExceeded', 'dailyLimitExceededUnreg',
        'rateLimitExceeded', 'userRateLimitExceeded', 'accessNotConfigured',
        'insufficientPermissions', 'forbidden', 'playlistItemsNotAccessible',
        'playlistNotFound', 'youtubeSignupRequired', 'authError',
        'invalidCredentials', 'accessDenied', 'backendError', 'notFound',
        'invalidValue', 'badRequest', 'unauthorized',
        'validation_error', 'unauthorized', 'restricted_resource',
        'object_not_found', 'rate_limited', 'internal_server_error',
        'service_unavailable', 'conflict_error',
    }
    try:
        body = response.json()
        if not isinstance(body, dict):
            return 'unrecognized API error'
        error = body.get('error', {})
        reasons = []
        if isinstance(error, dict):
            for entry in error.get('errors', []) or []:
                if isinstance(entry, dict) and entry.get('reason') in allowed:
                    reasons.append(entry['reason'])
        if body.get('code') in allowed:
            reasons.append(body['code'])
    except (ValueError, TypeError):
        return 'API returned a non-JSON error'
    reason = ', '.join(sorted(set(reasons))) or 'unrecognized API error'
    if any(r in reasons for r in ('quotaExceeded', 'dailyLimitExceeded', 'dailyLimitExceededUnreg')):
        reason += '; YouTube API quota exhausted. Stop this run and check the project quota before restarting.'
    elif 'accessNotConfigured' in reasons:
        reason += '; enable YouTube Data API v3 in the OAuth client project.'
    elif 'playlistItemsNotAccessible' in reasons:
        reason += '; the authorized account cannot access this playlist.'
    elif 'insufficientPermissions' in reasons:
        reason += '; check the OAuth scopes and authorized account.'
    return reason


def diagnose_youtube(playlist_index):
    """Read-only probe: no Notion calls and no caption requests."""
    if playlist_index < 1:
        raise SyncError('Playlist index must be 1 or greater.')
    if not os.environ.get('YOUTUBE_TOKEN_JSON'):
        os.environ['YOUTUBE_TOKEN_JSON'] = Path('youtube-token.json').read_text(encoding='utf-8')
    print('Checking YouTube OAuth and playlist-list access...', flush=True)
    api = youtube()
    available = list(yt_list(api, 'playlists', part='snippet', mine='true'))
    print(f'OAuth refresh and playlist listing succeeded: {len(available)} playlists.', flush=True)
    if playlist_index > len(available):
        raise SyncError('Requested playlist index is beyond the available playlists.')
    print(f'Checking playlistItems access for playlist {playlist_index} (one item only)...', flush=True)
    result = api.call('GET', 'playlistItems', params={
        'part': 'snippet', 'playlistId': available[playlist_index - 1]['id'],
        'maxResults': 1})
    print(f'Playlist-item access succeeded; returned {len(result.get("items", []))} item(s). No Notion records were changed.', flush=True)


class API:
    def __init__(self, base, headers, credentials=None):
        self.base, self.headers = base, headers
        self.credentials = credentials

    def recover_created_page(self, payload):
        # A lost response can still mean a successful creation. Never blindly POST again.
        parent = payload.get('parent', {})
        ds = parent.get('data_source_id')
        item_id = plain({'properties': payload.get('properties', {})}, 'Item ID')
        if not ds or not item_id:
            raise TemporaryAPIError('Creation outcome unknown; deferred')
        for delay in (5, 15, 30):
            progress.log(f'Checking whether Notion saved this video; waiting {delay}s')
            time.sleep(delay)
            matches = list(pages(self, ds, {
                'property': 'Item ID', 'rich_text': {'equals': item_id}}))
            if matches:
                if len(matches) > 1:
                    progress.log('Multiple saved copies found; using the most complete copy and preserving the others')
                progress.log('Recovered saved page after interrupted response')
                return preferred_copy(matches)
        raise TemporaryAPIError('Creation could not be confirmed; deferred without repeating POST')

    def call(self, method, path, **kwargs):
        safe = (method == 'GET' or
                (method == 'POST' and path.endswith('/query')) or
                (method == 'PATCH' and not path.endswith('/children')) or
                method == 'DELETE')
        service = 'Notion' if 'notion.com' in self.base else 'YouTube'
        for attempt in range(8):
            time.sleep(.36 if service == 'Notion' else .05)
            r = None
            try:
                if self.credentials is not None:
                    progress.log(f'{service} preparing authorization; request attempt {attempt + 1}/8')
                    self.credentials.before_request(oauth_request(), method, self.base + path, self.headers)
                progress.log(f'{service} {method} {path.split("/")[0]} - request attempt {attempt + 1}/8')
                r = requests.request(method, self.base + path, headers=self.headers,
                                     timeout=(10, 30), **kwargs)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, TransportError) as exc:
                reason = 'connection interrupted (' + type(exc).__name__ + ')' 
            else:
                if r.ok:
                    return r.json()
                if method == 'DELETE' and attempt > 0 and r.status_code == 404:
                    return {}
                if r.status_code != 429 and r.status_code < 500:
                    if service == 'Notion' and r.status_code == 400:
                        raise NotionValidationError(
                            f'API {method} {path} returned HTTP 400: {api_error_detail(r)}: '
                            + validation_message(r, self.headers))
                    if path == 'playlistItems' and r.status_code in (403, 404) and any(
                            code in api_error_detail(r) for code in ('playlistNotFound', 'playlistItemsNotAccessible')):
                        raise PlaylistUnavailable('YouTube playlist unavailable; preserving Notion entries and checkpoint')
                    raise SyncError(f'API {method} {path.split("?")[0]} returned HTTP {r.status_code}: {api_error_detail(r)}')
                reason = f'HTTP {r.status_code}'
            rate_limited = r is not None and r.status_code == 429
            if not safe and not rate_limited:
                if service == 'Notion' and method == 'POST' and path == 'pages':
                    return self.recover_created_page(kwargs.get('json', {}))
                # Appending blocks is not idempotent. Leave the completion marker unset;
                # the next run rebuilds this video's managed body from saved state.
                raise TemporaryAPIError(f'{service} {method} {path.split("?")[0]} write interrupted ({reason}); deferred')
            if attempt == 7:
                raise TemporaryAPIError(f'{service} unavailable after 8 attempts')
            delay = min(60, 5 * 2 ** attempt)
            if rate_limited:
                try:
                    delay = max(delay, float(r.headers.get('Retry-After', delay)))
                except (TypeError, ValueError):
                    pass
            if delay > 120:
                raise TemporaryAPIError(f'{service} requested a long retry delay; deferred until a later run')
            progress.log(f'{service} {reason}; retry {attempt + 1}/7 in {delay:g}s')
            # Short sleeps keep Ctrl+C responsive even for long Retry-After values.
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                time.sleep(max(0, min(1, deadline - time.monotonic())))
        raise TemporaryAPIError('API retry limit reached')


def notion():
    return API('https://api.notion.com/v1/', {
        'Authorization': 'Bearer ' + os.environ['NOTION_TOKEN'], 'Notion-Version': VERSION})


def oauth_request():
    # Google refresh otherwise uses a much longer default HTTP timeout.
    from google.auth.transport.requests import Request
    request = Request()
    def bounded_request(*args, **kwargs):
        kwargs['timeout'] = (10, 30)
        return request(*args, **kwargs)
    return bounded_request


def youtube():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    info = json.loads(os.environ['YOUTUBE_TOKEN_JSON'])
    c = Credentials.from_authorized_user_info(info)
    c.refresh(oauth_request())
    return API('https://www.googleapis.com/youtube/v3/', {'Authorization': 'Bearer ' + c.token}, credentials=c)


def yt_list(api, resource, **params):
    while True:
        data = api.call('GET', resource, params={**params, 'maxResults': 50})
        yield from data.get('items', [])
        if not data.get('nextPageToken'):
            break
        params['pageToken'] = data['nextPageToken']


def pages(api, ds, filter_=None):
    body = {'page_size': 100}
    if filter_:
        body['filter'] = filter_
    while True:
        data = api.call('POST', f'data_sources/{ds}/query', json=body)
        yield from data['results']
        if not data.get('has_more'):
            break
        body['start_cursor'] = data['next_cursor']


def children(api, block):
    params = {'page_size': 100}
    while True:
        data = api.call('GET', f'blocks/{block}/children', params=params)
        yield from data['results']
        if not data.get('has_more'):
            break
        params['start_cursor'] = data['next_cursor']


def write_body(api, page_id, text):
    # Only this dedicated toggle is owned by the importer. User notes stay intact.
    owned = [b for b in children(api, page_id) if b['type'] == 'toggle' and
             ''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                     for t in b['toggle']['rich_text']) == MARKER]
    for block in owned:
        api.call('DELETE', 'blocks/' + block['id'])
    result = api.call('PATCH', f'blocks/{page_id}/children', json={'children': [
        {'object': 'block', 'type': 'toggle', 'toggle': {'rich_text': rt(MARKER)}}]})
    target = result['results'][0]['id']
    blocks = [{'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': rt(text[i:i+1800])}}
              for i in range(0, len(text), 1800)]
    # Small batches keep UTF-8 request sizes under Notion's payload limit.
    for i in range(0, len(blocks), 20):
        api.call('PATCH', f'blocks/{target}/children', json={'children': blocks[i:i+20]})


class CaptionSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs['timeout'] = (10, 30)
        return super().request(method, url, **kwargs)


def transcript(video_id, config):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (TranscriptsDisabled, NoTranscriptFound,
        RequestBlocked, IpBlocked, VideoUnavailable)
    try:
        with CaptionSession() as session:
            result = YouTubeTranscriptApi(http_client=session).fetch(video_id, languages=config['languages'])
        text = '\n'.join(f'[{int(s.start)//60}:{int(s.start)%60:02d}] {s.text}' for s in result)
        limit = config.get('transcript_max_chars', 0)
        partial = limit > 0 and len(text) > limit
        return (text[:limit] if partial else text,
                'Partial' if partial else 'Full', result.language_code)
    except (RequestBlocked, IpBlocked):
        return '', 'Blocked', ''
    except TranscriptsDisabled:
        return '', 'No captions returned', ''
    except NoTranscriptFound:
        return '', 'No matching language', ''
    except VideoUnavailable:
        return '', 'Video unavailable', ''
    except Exception:
        return '', 'Error', ''


def should_fetch(page, now, config):
    if config['transcripts'] == 'off':
        return False
    if plain(page, 'Transcript status') in ('Full', 'Partial'):
        return False
    stamp = page.get('properties', {}).get('Transcript checked', {}).get('date')
    return not stamp or now - datetime.fromisoformat(stamp['start'].replace('Z', '+00:00')) >= timedelta(days=config['transcript_retry_days'])


def properties(item, playlist, video):
    s = item['snippet']
    v = video.get('snippet', {})
    vid = item['contentDetails']['videoId']
    thumbs = v.get('thumbnails', s.get('thumbnails', {}))
    thumb = next((thumbs[k]['url'] for k in ['maxres', 'standard', 'high', 'medium', 'default'] if k in thumbs), None)
    title = v.get('title', s.get('title', 'Unavailable video'))
    description = v.get('description', s.get('description', ''))
    props = {'Name': {'title': rt(title)}, 'Playlist': rich(playlist['snippet']['title']),
        'Playlist ID': rich(playlist['id']), 'Item ID': rich(item['id']), 'Video ID': rich(vid),
        'Channel': rich(v.get('channelTitle', s.get('videoOwnerChannelTitle', ''))),
        'Description': rich(description),
        'Video URL': {'url': 'https://www.youtube.com/watch?v=' + vid},
        'Playlist URL': {'url': 'https://www.youtube.com/playlist?list=' + playlist['id']},
        'Thumbnail': {'url': thumb}, 'Published': date(v.get('publishedAt', item['contentDetails'].get('videoPublishedAt'))),
        'Added': date(s.get('publishedAt')), 'Position': {'number': s.get('position', 0) + 1},
        'In playlist': {'checkbox': True}}
    return props, thumb, description


DB_PREFIX = 'youtube-notion-sync playlist: '


def playlist_databases(api, parent, defer_conflicts=False):
    found, grouped = {}, {}
    for block in children(api, parent):
        if block['type'] != 'child_database':
            continue
        db = api.call('GET', 'databases/' + block['id'])
        marker = ''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                         for t in db.get('description', []))
        if not marker.startswith(DB_PREFIX):
            continue
        key = marker[len(DB_PREFIX):]
        # A repeated block in pagination is not a second database.
        grouped.setdefault(key, {})[db['id']] = db
    for key, copies in grouped.items():
        if len(copies) > 1 or any(len(db.get('data_sources', [])) != 1 for db in copies.values()):
            destinations = ', '.join('https://www.notion.so/' + db_id.replace('-', '') for db_id in copies)
            reason = 'Duplicate playlist databases' if len(copies) > 1 else 'Managed database must have exactly one data source'
            conflict = PlaylistDatabaseConflict(f'{reason}; playlist {key}; databases: {destinations}')
            if not defer_conflicts:
                raise conflict
            progress.log(str(conflict) + '; preserving all copies and deferring this playlist')
            # Keep the key occupied so ensure_database never creates another copy.
            found[key] = conflict
        else:
            found[key] = next(iter(copies.values()))
    return found


def ensure_database(api, parent, playlist, found):
    key = playlist['id']
    title = playlist['snippet']['title']
    db = found.get(key)
    if isinstance(db, PlaylistDatabaseConflict):
        raise db
    if db is None:
        db = api.call('POST', 'databases', json={
            'parent': {'type': 'page_id', 'page_id': parent},
            'title': rt(title), 'description': rt(DB_PREFIX + key),
            'is_inline': False, 'initial_data_source': {'properties': SCHEMA}})
        found[key] = db
    else:
        old_title = ''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                            for t in db.get('title', []))
        if old_title != title:
            api.call('PATCH', 'databases/' + db['id'], json={'title': rt(title)})
    return db['data_sources'][0]['id']



GALLERY_NAME = 'Video cards'
GALLERY_FIELDS = ['Name', 'OPEN VIDEO', 'Channel', 'Description', 'Published', 'Added']


def ensure_gallery(api, database_id, data_source_id):
    """Create gallery, and migrate existing cards to expose the video link."""
    schema = api.call('GET', 'data_sources/' + data_source_id)['properties']
    if ('OPEN VIDEO' not in schema or
            schema['OPEN VIDEO'].get('formula', {}).get('expression') != OPEN_VIDEO_FORMULA):
        schema = api.call('PATCH', 'data_sources/' + data_source_id, json={
            'properties': {'OPEN VIDEO': SCHEMA['OPEN VIDEO']}})['properties']
    link_id = schema['OPEN VIDEO']['id']
    params = {'database_id': database_id, 'page_size': 100}
    while True:
        response = api.call('GET', 'views', params=params)
        for ref in response.get('results', []):
            view = api.call('GET', 'views/' + ref['id'])
            if view.get('name') == GALLERY_NAME:
                if view.get('type') != 'gallery':
                    raise SyncError('Video cards view exists with a different type')
                config = view.get('configuration') or {}
                props = config.get('properties')
                if props is None:
                    props = [{'property_id': schema[name]['id'], 'visible': True}
                             for name in GALLERY_FIELDS if name != 'OPEN VIDEO']
                else:
                    props = [dict(p) for p in props]
                link_prop = next((p for p in props if p['property_id'] == link_id), None)
                if link_prop is None or not link_prop.get('visible', False):
                    props = [p for p in props if p['property_id'] != link_id]
                    title_index = next((i for i, p in enumerate(props)
                                        if p['property_id'] == schema['Name']['id']), -1)
                    props.insert(title_index + 1, {'property_id': link_id, 'visible': True})
                    api.call('PATCH', 'views/' + view['id'], json={
                        'configuration': {'type': 'gallery', 'properties': props}})
                return view['id']
        if not response.get('has_more'):
            break
        params['start_cursor'] = response['next_cursor']
    visible = [{'property_id': schema[name]['id'], 'visible': True}
               for name in GALLERY_FIELDS]
    hidden = [{'property_id': value['id'], 'visible': False}
              for name, value in schema.items() if name not in GALLERY_FIELDS]
    result = api.call('POST', 'views', json={
        'database_id': database_id, 'data_source_id': data_source_id,
        'name': GALLERY_NAME, 'type': 'gallery', 'position': {'type': 'start'},
        'filter': {'property': 'In playlist', 'checkbox': {'equals': True}},
        'sorts': [{'property': 'Added', 'direction': 'descending'}],
        'configuration': {
            'type': 'gallery', 'properties': visible + hidden,
            'cover': {'type': 'page_cover'}, 'cover_size': 'medium',
            'cover_aspect': 'contain', 'card_layout': 'list'}})
    return result['id']


CHECKPOINT_VERSION = 2
DEFAULT_CHECKPOINT = '.youtube-sync-checkpoint.json'


def checkpoint_path(config):
    return Path(config.get('checkpoint_file', DEFAULT_CHECKPOINT))


def checkpoint_context(config, available):
    # Bind resumptions to OAuth identity and sync-affecting settings without saving secrets.
    info = json.loads(os.environ.get('YOUTUBE_TOKEN_JSON', '{}'))
    identity = {k: info.get(k) for k in ('client_id', 'refresh_token')}
    settings = {k: v for k, v in config.items() if k not in
                ('checkpoint_file', 'checkpoint_storage')}
    settings['playlist_ids'] = sorted(settings.get('playlist_ids', []))
    channels = sorted({p.get('snippet', {}).get('channelId', '') for p in available})
    return hashlib.sha256(json.dumps([identity, settings, channels], sort_keys=True).encode()).hexdigest()


def validate_checkpoint(state):
    if not isinstance(state, dict):
        raise SyncError('Invalid checkpoint structure; rename the checkpoint to start a full scan.')
    for key in ('playlist_ids', 'completed_playlist_ids'):
        values = state.get(key)
        if (not isinstance(values, list) or
                not all(isinstance(v, str) and v for v in values) or
                len(values) != len(set(values))):
            raise SyncError('Invalid checkpoint playlist list; rename the checkpoint to start a full scan.')
    if not set(state['completed_playlist_ids']).issubset(state['playlist_ids']):
        raise SyncError('Invalid checkpoint completion list; rename the checkpoint to start a full scan.')


def load_checkpoint(path, parent, selected_ids, context=None):
    state = None
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncError('Checkpoint is unreadable; rename it to start a full scan.') from exc
        validate_checkpoint(state)
        if state.get('parent_page_id') != parent:
            raise SyncError('Checkpoint belongs to a different destination; use a different checkpoint file.')
        if state.get('version') != CHECKPOINT_VERSION or state.get('context') != context:
            progress.log('Checkpoint version, account, or settings changed; starting a fresh full scan')
            state = None
    if state is None:
        state = {'version': CHECKPOINT_VERSION, 'parent_page_id': parent,
                 'context': context, 'playlist_ids': selected_ids,
                 'completed_playlist_ids': [], 'pass_id': uuid.uuid4().hex}
        save_checkpoint(path, state)
        progress.log(f'New checkpoint; starting a full scan at playlist 1/{len(selected_ids)}')
        return state
    state.setdefault('pass_id', uuid.uuid4().hex)
    now = time.time()
    stamps = state.setdefault('completed_at', {})
    generations = state.setdefault('playlist_generations', {})
    # Revisit completed playlists after the normal schedule interval even if one
    # permanently unavailable playlist keeps the pass unfinished.
    completed = set(state['completed_playlist_ids'])
    expired = set()
    for pid in list(completed):
        stamp = stamps.setdefault(pid, now)
        if now - stamp >= 6 * 3600:
            completed.remove(pid)
            expired.add(pid)
            stamps.pop(pid, None)
            generations[pid] = generations.get(pid, 0) + 1
    snapshot = [pid for pid in state['playlist_ids'] if pid in selected_ids]
    snapshot.extend(pid for pid in selected_ids if pid not in snapshot)
    snapshot = [pid for pid in snapshot if pid not in expired] + [pid for pid in snapshot if pid in expired]
    state['playlist_ids'] = snapshot
    state['completed_playlist_ids'] = [pid for pid in snapshot if pid in completed]
    save_checkpoint(path, state)
    remaining = len(snapshot) - len(state['completed_playlist_ids'])
    progress.log(f'Resuming checkpoint: {len(state["completed_playlist_ids"])} playlists complete, {remaining} remaining')
    return state


def save_checkpoint(path, state):
    if isinstance(path, NotionCheckpoint):
        path.save(state)
        return
    temporary = path.with_name(path.name + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write(json.dumps(state, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise SyncError('Could not save checkpoint; stopped to avoid losing the position.') from exc


class NotionCheckpoint:
    """Private append-only snapshots survive new Actions runners and timeouts."""
    TITLE = 'YouTube sync checkpoint (managed)'
    PREFIX = 'Checkpoint snapshot '

    def __init__(self, api, parent):
        self.api = api
        matches = [b for b in children(api, parent) if b['type'] == 'child_page'
                   and b['child_page']['title'] == self.TITLE]
        if len(matches) > 1:
            raise SyncError('Multiple checkpoint pages found; resolve before syncing.')
        self.page_id = matches[0]['id'] if matches else api.call('POST', 'pages', json={
            'parent': {'page_id': parent},
            'properties': {'title': {'title': rt(self.TITLE)}}})['id']
        self.snapshots = [b for b in children(api, self.page_id) if b['type'] == 'toggle'
                          and self.label(b).startswith(self.PREFIX)]
        self.value = None
        if self.snapshots:
            latest = self.snapshots[-1]
            parts = list(children(api, latest['id']))
            text = ''.join(''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                           for t in b.get('paragraph', {}).get('rich_text', [])) for b in parts)
            digest = hashlib.sha256(text.encode()).hexdigest()
            if self.label(latest) != self.PREFIX + digest:
                raise SyncError('Remote checkpoint snapshot is incomplete; inspect its managed page before resuming.')
            self.value = json.loads(text)

    @staticmethod
    def label(block):
        return ''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                       for t in block['toggle']['rich_text'])

    def exists(self):
        return self.value is not None

    def read_text(self, encoding='utf-8'):
        return json.dumps(self.value)

    def save(self, state):
        text = json.dumps(state, separators=(',', ':'))
        chunks = [text[i:i+1800] for i in range(0, len(text), 1800)]
        if len(chunks) > 90:
            raise SyncError('Remote checkpoint exceeds supported snapshot size.')
        label = self.PREFIX + hashlib.sha256(text.encode()).hexdigest()
        response = self.api.call('PATCH', f'blocks/{self.page_id}/children', json={'children': [{
            'object': 'block', 'type': 'toggle', 'toggle': {
                'rich_text': rt(label), 'children': [
                    {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': rt(chunk)}}
                    for chunk in chunks]}}]})
        self.value = state
        self.snapshots.extend(response['results'])
        # Keep the last two complete snapshots. Never touch unrecognized user blocks.
        while len(self.snapshots) > 2:
            oldest = self.snapshots[0]
            self.api.call('DELETE', 'blocks/' + oldest['id'])
            self.snapshots.pop(0)

    def unlink(self):
        self.save(None)

    def __str__(self):
        return 'the managed checkpoint page in Notion'


def mark_playlist_complete(path, state, playlist_id):
    completed = state['completed_playlist_ids']
    if playlist_id not in completed:
        completed.append(playlist_id)
        state.setdefault('completed_at', {})[playlist_id] = time.time()
        save_checkpoint(path, state)


class VideoCheckpoint:
    """Local SQLite transactions retain metadata and individual completion markers."""
    def __init__(self, path, pass_id, playlist_id, ds):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        identity = [pass_id, playlist_id, ds]
        if self.get('identity') != identity:
            with self.db:
                self.db.execute('DELETE FROM cache')
                self.set('identity', identity)

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM cache WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES (?, ?)', (key, json.dumps(value)))

    def close(self):
        self.db.close()


def run(config):
    progress.log('Connecting to YouTube and discovering playlists')
    api = notion()
    y = youtube()
    parent = os.environ.get('NOTION_PARENT_PAGE_ID') or config['notion_parent_page_id']
    available = list(yt_list(y, 'playlists', part='snippet', mine='true'))
    requested = set(config['playlist_ids'])
    if requested - {p['id'] for p in available}:
        raise SyncError('Configured playlists are not accessible to this Google account')
    selected = [p for p in available if not requested or p['id'] in requested]
    by_id = {p['id']: p for p in selected}
    storage = config.get('checkpoint_storage') or ('notion' if os.environ.get('GITHUB_ACTIONS') else 'local')
    if storage not in ('local', 'notion'):
        raise SyncError('checkpoint_storage must be local or notion.')
    cp_path = NotionCheckpoint(api, parent) if storage == 'notion' else checkpoint_path(config)
    state = load_checkpoint(cp_path, parent, [p['id'] for p in selected],
                            checkpoint_context(config, available))
    completed = set(state['completed_playlist_ids'])
    queue = [pid for pid in state['playlist_ids'] if pid in by_id and pid not in completed]

    progress.log('Finding existing playlist databases in Notion')
    found = playlist_databases(api, parent, defer_conflicts=True)
    totals = {}
    progress.log(f'Found {len(selected)} playlists; {len(queue)} remain in this pass; transcript budget: {config["transcript_budget"]} per playlist')
    for pass_number, playlist_id in enumerate(queue, 1):
        playlist = by_id[playlist_id]
        original_number = next(i for i, p in enumerate(selected, 1) if p['id'] == playlist_id)
        label = f'Playlist {original_number}/{len(selected)} (remaining {pass_number}/{len(queue)})'
        if not os.environ.get('GITHUB_ACTIONS'):
            label += ': ' + str(playlist['snippet']['title']).encode('ascii', 'backslashreplace').decode('ascii').replace('\n', ' ')
        progress.log(label + ' - preparing database and gallery')
        snapshot = None
        try:
            ds = ensure_database(api, parent, playlist, found)
            view_api = API(api.base, {**api.headers, 'Notion-Version': '2026-03-11'})
            ensure_gallery(view_api, found[playlist_id]['id'], ds)
            if storage == 'local':
                cache_path = checkpoint_path(config).with_name(
                    checkpoint_path(config).name + '.' + hashlib.sha256(playlist_id.encode()).hexdigest()[:16] + '.sqlite')
                snapshot = VideoCheckpoint(cache_path, [state['pass_id'], state.get('playlist_generations', {}).get(playlist_id, 0)], playlist_id, ds)
            counts = run_single({**config, 'playlist_ids': [playlist_id], '_video_checkpoint': snapshot}, ds,
                                playlist=playlist, y=y)
        except (PlaylistUnavailable, TemporaryAPIError, PlaylistDatabaseConflict) as exc:
            progress.log(f'{label} - {exc}; left pending, continuing to next playlist')
            totals['deferred_playlists'] = totals.get('deferred_playlists', 0) + 1
            continue
        finally:
            if snapshot is not None:
                snapshot.close()
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
        if counts.get('deferred_writes'):
            progress.log(f'Playlist {original_number}/{len(selected)} has deferred writes; leaving it incomplete in the checkpoint')
        else:
            mark_playlist_complete(cp_path, state, playlist_id)
            completed.add(playlist_id)
            if snapshot is not None:
                cache_path.unlink(missing_ok=True)
            progress.log(f'Checkpoint saved: {len(completed)}/{len(selected)} playlists complete')

    report_results(totals)
    incomplete = [pid for pid in state['playlist_ids'] if pid in by_id and pid not in set(state['completed_playlist_ids'])]
    if incomplete:
        progress.log(f'Import pass finished; {len(incomplete)} playlists remain in {cp_path}')
    else:
        try:
            cp_path.unlink()
        except FileNotFoundError:
            pass
        progress.log('Full scan finished; checkpoint cleared. The next run will start a new full scan.')



def report_results(counts):
    """Aggregate only: no private titles or transcript text in Actions logs."""
    print(json.dumps({'run_totals': counts}))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write('## YouTube import results\n\n')
            f.write('Counts describe playlist entries; videos in multiple playlists count more than once.\n\n')
            f.write('| Metric | Count |\n| --- | ---: |\n')
            for key, value in sorted(counts.items()):
                f.write(f'| {key} | {value} |\n')
            f.write('\nPending entries have not necessarily failed: they may be waiting for the attempt budget or a later run.\n')
            f.write('Blocked does not prove a permanent video restriction; it can reflect the runner IP or request limiting.\n')


def run_single(config, ds, playlist=None, y=None):
    progress.log('Connecting and reading existing Notion entries')
    n, y = notion(), y or youtube()
    existing, duplicate_rows = index_existing(pages(n, ds))
    progress.log(f'Read {len(existing)} existing Notion entries; locating playlist')
    if playlist is not None:
        playlists = [playlist]
    else:
        playlists = list(yt_list(y, 'playlists', part='snippet', mine='true'))
        requested = set(config['playlist_ids'])
        if requested:
            missing = requested - {p['id'] for p in playlists}
            if missing:
                raise SyncError('Configured playlists are not accessible to this Google account')
            playlists = [p for p in playlists if p['id'] in requested]
    now = datetime.now(timezone.utc)
    transcript_cache = {}
    counts = {'created': 0, 'updated': 0, 'removed': 0, 'transcript_attempts': 0,
              'duplicate_rows_preserved': duplicate_rows}
    counts.update({'entries_scanned': 0, 'saved_transcripts': 0, 'pending_transcripts': 0,
                   'unsuccessful_transcripts': 0})
    blocked = False
    snapshot = config.get('_video_checkpoint')
    for playlist in playlists:
        # Complete pagination before reconciling removals.
        progress.log('Reading playlist items from YouTube')
        saved = snapshot.get('source') if snapshot else None
        if saved is None:
            items = list(yt_list(y, 'playlistItems', part='snippet,contentDetails', playlistId=playlist['id']))
            # Publish only a complete listing. Never reconcile removals after partial pagination.
            if snapshot:
                snapshot.set('source', {'items': items, 'baseline': list(existing)})
        else:
            items = saved['items']
            progress.log(f'Reusing local playlist snapshot: {len(items)} entries')
        progress.log(f'Found {len(items)} playlist entries; fetching video metadata')
        ids = list(dict.fromkeys(i['contentDetails']['videoId'] for i in items))
        videos = {}
        for offset in range(0, len(ids), 50):
            progress.log(f'Fetching metadata {offset + 1}-{min(offset + 50, len(ids))}/{len(ids)}')
            batch_key = 'metadata:' + str(offset)
            result = snapshot.get(batch_key) if snapshot else None
            if result is None:
                result = y.call('GET', 'videos', params={'part': 'snippet', 'id': ','.join(ids[offset:offset+50])})
                if snapshot:
                    snapshot.set(batch_key, result)
            videos.update({v['id']: v for v in result['items']})
        seen = set()
        for item_number, item in enumerate(items, 1):
            progress.log(f'Video {item_number}/{len(items)} - checking saved state')
            seen.add(item['id'])
            if snapshot and snapshot.get('done:' + item['id']):
                counts['checkpoint_skipped'] = counts.get('checkpoint_skipped', 0) + 1
                progress.log(f'Video {item_number}/{len(items)} - already completed in this pass, skipped')
                continue
            old = existing.get(item['id'], {})
            vid = item['contentDetails']['videoId']
            props, thumb, description = properties(item, playlist, videos.get(vid, {}))
            digest = hashlib.sha256(json.dumps(props, sort_keys=True).encode()).hexdigest()
            old_status = plain(old, 'Transcript status')
            fetch = should_fetch(old, now, config) and not blocked
            fetched = snapshot.get('caption:' + item['id']) if snapshot else None
            if fetched is not None:
                progress.log(f'Video {item_number}/{len(items)} - reusing captions awaiting save')
            if fetched is None and fetch and (vid in transcript_cache or counts['transcript_attempts'] < config['transcript_budget']):
                if vid not in transcript_cache:
                    progress.log(f'Video {item_number}/{len(items)} - fetching captions (attempt {counts["transcript_attempts"] + 1}/{config["transcript_budget"]})')
                    transcript_cache[vid] = transcript(vid, config)
                    counts['transcript_attempts'] += 1
                    time.sleep(1)
                fetched = transcript_cache[vid]
                blocked = fetched[1] == 'Blocked'
                progress.log(f'Video {item_number}/{len(items)} - captions: {fetched[1]}')
                if blocked:
                    progress.log('Caption requests blocked; continuing metadata for this playlist')
                elif counts['transcript_attempts'] == config['transcript_budget']:
                    progress.log('Caption budget reached; continuing metadata for this playlist')
            if fetched is not None and snapshot:
                snapshot.set('caption:' + item['id'], fetched)
            status_now = fetched[1] if fetched else (old_status or
                ('Disabled' if config['transcripts'] == 'off' else 'Pending'))
            counts['entries_scanned'] += 1
            metric = ('saved_transcripts' if status_now in ('Full', 'Partial') else
                      'pending_transcripts' if status_now in ('Pending', 'Disabled') else
                      'unsuccessful_transcripts')
            counts[metric] += 1
            status_key = 'status: ' + status_now
            counts[status_key] = counts.get(status_key, 0) + 1
            if old and plain(old, 'Content hash') == digest and fetched is None:
                if snapshot:
                    snapshot.set('done:' + item['id'], True)
                progress.log(f'Video {item_number}/{len(items)} - unchanged, skipped; captions={status_now}')
                continue
            progress.log(f'Video {item_number}/{len(items)} - saving to Notion')
            try:
                if old:
                    page_id = old['id']
                    n.call('PATCH', 'pages/' + page_id, json={'properties': {**props, 'Content hash': rich('')}, 'cover':
                        {'type': 'external', 'external': {'url': thumb}} if thumb else None})
                    counts['updated'] += 1
                else:
                    payload = {'parent': {'type': 'data_source_id', 'data_source_id': ds}, 'properties': props}
                    if thumb:
                        payload['cover'] = {'type': 'external', 'external': {'url': thumb}}
                    page_id = n.call('POST', 'pages', json=payload)['id']
                    counts['created'] += 1
                finish = {'Content hash': rich(digest)}
                if fetched:
                    text, status, language = fetched
                    finish.update({'Transcript status': rich(status), 'Transcript language': rich(language),
                                   'Transcript checked': date(now.isoformat())})
                    write_body(n, page_id, 'Transcript\n\n' + (text or 'Transcript ' + status.lower() + '.'))
                elif not old_status:
                    status = 'Disabled' if config['transcripts'] == 'off' else 'Pending'
                    finish['Transcript status'] = rich(status)
                    write_body(n, page_id, 'Transcript ' + status.lower() + '.')
                # Commit completion after block writes. Failed runs resume on the next run.
                n.call('PATCH', 'pages/' + page_id, json={'properties': finish})
                if snapshot:
                    snapshot.set('done:' + item['id'], True)
                    snapshot.set('deferred:' + item['id'], None)
                progress.log(f'Video {item_number}/{len(items)} - saved; captions={status_now}; created={counts["created"]}, updated={counts["updated"]}, transcripts={counts["saved_transcripts"]}')
            except (TemporaryAPIError, NotionValidationError) as exc:
                counts['deferred_writes'] = counts.get('deferred_writes', 0) + 1
                # A fetched caption is only "saved" once the completion write succeeds.
                counts[metric] -= 1
                counts[status_key] -= 1
                if isinstance(exc, NotionValidationError):
                    counts['validation_errors'] = counts.get('validation_errors', 0) + 1
                if snapshot:
                    snapshot.set('deferred:' + item['id'], {
                        'video_id': vid, 'error': str(exc), 'at': now.isoformat()})
                progress.log(f'Video {item_number}/{len(items)} (video {vid}, item {item["id"]}) - {exc}; deferred to next run, continuing')
                continue
        progress.log('Checking for entries removed from playlist')
        baseline = set(snapshot.get('source')['baseline']) if snapshot else set(existing)
        for key, old in existing.items():
            if key not in baseline:
                continue
            if plain(old, 'Playlist ID') == playlist['id'] and key not in seen and old['properties']['In playlist']['checkbox']:
                n.call('PATCH', 'pages/' + old['id'], json={'properties': {
                    'In playlist': {'checkbox': False}, 'Content hash': rich('')}})
                counts['removed'] += 1
    print(json.dumps(counts))
    return counts


def search_single(query, ds):
    """Exhaustive property + block search, no title-only API assumption."""
    n = notion()
    q = query.casefold()
    def walk(block):
        for b in children(n, block):
            value = b.get(b['type'], {})
            yield ''.join(x.get('plain_text', x.get('text', {}).get('content', '')) for x in value.get('rich_text', []))
            if b.get('has_children'):
                yield from walk(b['id'])
    for p in pages(n, ds):
        text = '\n'.join([plain(p, k) for k in ['Name', 'Playlist', 'Channel', 'Description']] + list(walk(p['id'])))
        index = text.casefold().find(q)
        if index >= 0:
            print(json.dumps({'title': plain(p, 'Name'), 'playlist': plain(p, 'Playlist'),
                'url': p['url'], 'excerpt': text[max(0, index-100):index+400]}, ensure_ascii=False))


def main():
    load_local_environment()
    print('YouTube Notion Sync build 2026-09-22-transcript-backfill-v1', flush=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['auth', 'playlists', 'sync', 'search', 'diagnose', 'transcripts'])
    parser.add_argument('--max-attempts', type=int, default=250)
    parser.add_argument('--delay', type=float, default=45)
    parser.add_argument('--no-wait-on-block', action='store_true', help='Exit caption fetching during a persisted block instead of automatically waiting')
    parser.add_argument('--transcripts', choices=['off', 'best-effort'])
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--query')
    parser.add_argument('--playlist-index', type=int, default=2)
    args = parser.parse_args()
    if args.command == 'transcripts':
        from transcript_backfill import run as backfill
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        with progress:
            backfill(config, args.max_attempts, args.delay, wait_on_block=not args.no_wait_on_block)
    elif args.command == 'diagnose':
        diagnose_youtube(args.playlist_index)
    elif args.command == 'auth':
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file('client_secret.json',
            scopes=['https://www.googleapis.com/auth/youtube.readonly'])
        creds = flow.run_local_server(port=0, access_type='offline', prompt='consent')
        fd = os.open('youtube-token.json', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(creds.to_json())
        print('Saved youtube-token.json. Store it as a GitHub secret; do not commit it.')
    elif args.command == 'playlists':
        for p in yt_list(youtube(), 'playlists', part='snippet,status', mine='true'):
            print(json.dumps({'id': p['id'], 'name': p['snippet']['title'], 'privacy': p['status']['privacyStatus']}))
    elif args.command == 'search':
        if not args.query:
            parser.error('--query is required')
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        parent = os.environ.get('NOTION_PARENT_PAGE_ID') or config['notion_parent_page_id']
        for db in playlist_databases(notion(), parent).values():
            search_single(args.query, db['data_sources'][0]['id'])
    else:
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        if args.transcripts:
            config['transcripts'] = args.transcripts
        with progress:
            run(config)


if __name__ == '__main__':
    # Backfill imports sync: share this module and its live heartbeat instance.
    sys.modules['sync'] = sys.modules[__name__]
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped. Run again to resume from saved Notion records.', file=sys.stderr, flush=True)
        sys.exit(130)
    except Exception as exc:
        # Avoid dumping OAuth tokens, private titles, API response bodies in CI logs.
        print(f'Failed: {error_message(exc)}', file=sys.stderr, flush=True)
        sys.exit(1)
