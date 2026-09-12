"""Private YouTube playlist → Notion sync. Python 3.11+."""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

VERSION = '2025-09-03'
MARKER = 'YouTube importer content (managed)'
SCHEMA = {'Name': {'title': {}}, **{k: {'rich_text': {}} for k in
    ['Playlist', 'Playlist ID', 'Item ID', 'Video ID', 'Channel', 'Description',
     'Content hash', 'Transcript status', 'Transcript language']},
    **{k: {'url': {}} for k in ['Video URL', 'Playlist URL', 'Thumbnail']},
    **{k: {'date': {}} for k in ['Published', 'Added', 'Transcript checked']},
    'Position': {'number': {}}, 'In playlist': {'checkbox': {}}}


def rt(text):
    return [{'type': 'text', 'text': {'content': text[i:i+1800]}}
            for i in range(0, len(text), 1800)]


def rich(text):
    return {'rich_text': rt(text)}


def plain(page, name):
    p = page.get('properties', {}).get(name, {})
    return ''.join(x.get('plain_text', x.get('text', {}).get('content', ''))
                   for x in p.get('rich_text', p.get('title', [])))


def date(value):
    return {'date': {'start': value} if value else None}


class API:
    def __init__(self, base, headers):
        self.base, self.headers = base, headers

    def call(self, method, path, **kwargs):
        for attempt in range(6):
            time.sleep(.36 if 'notion.com' in self.base else .05)
            # Do not retry ambiguous writes: the next run reconciles Notion state.
            r = requests.request(method, self.base + path, headers=self.headers,
                                 timeout=60, **kwargs)
            if r.status_code == 429 or (method == 'GET' and r.status_code >= 500):
                if attempt < 5:
                    time.sleep(min(30, float(r.headers.get('Retry-After', 2 ** attempt))))
                    continue
            if not r.ok:
                raise RuntimeError(f'API {method} {path.split("?")[0]} returned HTTP {r.status_code}')
            return r.json()
        raise RuntimeError('API retry limit reached')


def notion():
    return API('https://api.notion.com/v1/', {
        'Authorization': 'Bearer ' + os.environ['NOTION_TOKEN'], 'Notion-Version': VERSION})


def youtube():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    info = json.loads(os.environ['YOUTUBE_TOKEN_JSON'])
    c = Credentials.from_authorized_user_info(info)
    c.refresh(Request())
    return API('https://www.googleapis.com/youtube/v3/', {'Authorization': 'Bearer ' + c.token})


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


def transcript(video_id, config):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (TranscriptsDisabled, NoTranscriptFound,
        RequestBlocked, IpBlocked, VideoUnavailable)
    try:
        result = YouTubeTranscriptApi().fetch(video_id, languages=config['languages'])
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


def playlist_databases(api, parent):
    found = {}
    for block in children(api, parent):
        if block['type'] != 'child_database':
            continue
        db = api.call('GET', 'databases/' + block['id'])
        marker = ''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                         for t in db.get('description', []))
        if not marker.startswith(DB_PREFIX):
            continue
        key = marker[len(DB_PREFIX):]
        if key in found:
            raise RuntimeError('Duplicate playlist databases; resolve before syncing')
        if len(db.get('data_sources', [])) != 1:
            raise RuntimeError('Managed database must have exactly one data source')
        found[key] = db
    return found


def ensure_database(api, parent, playlist, found):
    key = playlist['id']
    title = playlist['snippet']['title']
    db = found.get(key)
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
GALLERY_FIELDS = ['Name', 'Channel', 'Description', 'Published', 'Added']


def ensure_gallery(api, database_id, data_source_id):
    """Create once, including recovery after a database-only partial run."""
    params = {'database_id': database_id, 'page_size': 100}
    while True:
        response = api.call('GET', 'views', params=params)
        for ref in response.get('results', []):
            view = api.call('GET', 'views/' + ref['id'])
            if view.get('name') == GALLERY_NAME:
                if view.get('type') != 'gallery':
                    raise RuntimeError('Video cards view exists with a different type')
                return view['id']
        if not response.get('has_more'):
            break
        params['start_cursor'] = response['next_cursor']
    schema = api.call('GET', 'data_sources/' + data_source_id)['properties']
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


def run(config):
    api = notion()
    parent = os.environ.get('NOTION_PARENT_PAGE_ID') or config['notion_parent_page_id']
    available = list(yt_list(youtube(), 'playlists', part='snippet', mine='true'))
    requested = set(config['playlist_ids'])
    if requested - {p['id'] for p in available}:
        raise RuntimeError('Configured playlists are not accessible to this Google account')
    found = playlist_databases(api, parent)
    totals = {}
    for playlist in available:
        if requested and playlist['id'] not in requested:
            continue
        ds = ensure_database(api, parent, playlist, found)
        view_api = API(api.base, {**api.headers, 'Notion-Version': '2026-03-11'})
        ensure_gallery(view_api, found[playlist['id']]['id'], ds)
        counts = run_single({**config, 'playlist_ids': [playlist['id']]}, ds)
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
    report_results(totals)



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


def run_single(config, ds):
    n, y = notion(), youtube()
    existing = {}
    for p in pages(n, ds):
        key = plain(p, 'Item ID')
        if key in existing:
            raise RuntimeError('Duplicate Item IDs in database; resolve before syncing')
        if key:
            existing[key] = p
    playlists = list(yt_list(y, 'playlists', part='snippet', mine='true'))
    requested = set(config['playlist_ids'])
    if requested:
        missing = requested - {p['id'] for p in playlists}
        if missing:
            raise RuntimeError('Configured playlists are not accessible to this Google account')
        playlists = [p for p in playlists if p['id'] in requested]
    now = datetime.now(timezone.utc)
    transcript_cache = {}
    counts = {'created': 0, 'updated': 0, 'removed': 0, 'transcript_attempts': 0}
    counts.update({'entries_scanned': 0, 'saved_transcripts': 0, 'pending_transcripts': 0,
                   'unsuccessful_transcripts': 0})
    blocked = False
    for playlist in playlists:
        # Complete pagination before reconciling removals.
        items = list(yt_list(y, 'playlistItems', part='snippet,contentDetails', playlistId=playlist['id']))
        ids = list(dict.fromkeys(i['contentDetails']['videoId'] for i in items))
        videos = {}
        for offset in range(0, len(ids), 50):
            result = y.call('GET', 'videos', params={'part': 'snippet', 'id': ','.join(ids[offset:offset+50])})
            videos.update({v['id']: v for v in result['items']})
        seen = set()
        for item in items:
            seen.add(item['id'])
            old = existing.get(item['id'], {})
            vid = item['contentDetails']['videoId']
            props, thumb, description = properties(item, playlist, videos.get(vid, {}))
            digest = hashlib.sha256(json.dumps(props, sort_keys=True).encode()).hexdigest()
            old_status = plain(old, 'Transcript status')
            fetch = should_fetch(old, now, config) and not blocked
            fetched = None
            if fetch and (vid in transcript_cache or counts['transcript_attempts'] < config['transcript_budget']):
                if vid not in transcript_cache:
                    transcript_cache[vid] = transcript(vid, config)
                    counts['transcript_attempts'] += 1
                    time.sleep(1)
                fetched = transcript_cache[vid]
                blocked = fetched[1] == 'Blocked'
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
                continue
            if old:
                page_id = old['id']
                n.call('PATCH', 'pages/' + page_id, json={'properties': props, 'cover':
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
        for key, old in existing.items():
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
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['auth', 'playlists', 'sync', 'search'])
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--query')
    args = parser.parse_args()
    if args.command == 'auth':
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
        config = json.loads(Path(args.config).read_text())
        parent = os.environ.get('NOTION_PARENT_PAGE_ID') or config['notion_parent_page_id']
        for db in playlist_databases(notion(), parent).values():
            search_single(args.query, db['data_sources'][0]['id'])
    else:
        config = json.loads(Path(args.config).read_text())
        run(config)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Avoid dumping OAuth tokens, private titles, API response bodies in CI logs.
        print(f'Failed ({type(exc).__name__}). Check credentials, permissions, configuration and API status.', file=sys.stderr)
        sys.exit(1)
