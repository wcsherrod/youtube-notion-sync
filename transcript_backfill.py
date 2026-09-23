"""Resumable, local transcript backfill; no YouTube Data API calls."""
import json
import math
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import sync


def wait_until(deadline, reason):
    if deadline > time.time():
        resume = datetime.fromtimestamp(deadline, timezone.utc).isoformat()
        sync.progress.log(f'Transcripts: {reason}; automatically resuming at {resume}')
    while deadline > time.time():
        time.sleep(min(20, deadline - time.time()))


def run(config, max_attempts=250, delay=45, journal='youtube-transcripts.sqlite', wait_on_block=True):
    if max_attempts < 1 or not math.isfinite(delay) or delay < 1:
        raise sync.SyncError('Transcript attempts must be positive and delay at least one second.')
    parent = sync.os.environ.get('NOTION_PARENT_PAGE_ID') or config['notion_parent_page_id']
    languages = config.get('languages', ['en'])
    scope = json.dumps([parent, languages], sort_keys=True)
    db = sqlite3.connect(Path(journal))
    db.execute('CREATE TABLE IF NOT EXISTS results (scope TEXT, video TEXT, value TEXT, PRIMARY KEY(scope, video))')
    db.execute('CREATE TABLE IF NOT EXISTS settings (scope TEXT PRIMARY KEY, blocked_until REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS delivered (scope TEXT, page TEXT, fetched REAL, PRIMARY KEY(scope, page))')
    db.execute('CREATE TABLE IF NOT EXISTS throttle (scope TEXT PRIMARY KEY, strikes INTEGER, next_request REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS candidate_snapshots (scope TEXT PRIMARY KEY, fetched REAL, value TEXT)')
    totals = dict(attempts=0, saved_pages=0, deferred_pages=0, cached=0)
    cooldown = max(1, config.get('transcript_retry_days', 7)) * 86400
    try:
        blocked = db.execute('SELECT blocked_until FROM settings WHERE scope=?', (scope,)).fetchone()
        blocked_until = blocked[0] if blocked else 0
        throttle = db.execute('SELECT strikes, next_request FROM throttle WHERE scope=?', (scope,)).fetchone()
        strikes, next_request = throttle if throttle else (0, 0)
        if blocked and not throttle:
            # Migrate the old seven-day global block to the first 30-minute cooldown.
            blocked_until = min(blocked_until, time.time() + 1800)
            strikes = 1
            with db:
                db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (scope, blocked_until))
                db.execute('INSERT OR REPLACE INTO throttle VALUES (?, ?, ?)', (scope, strikes, next_request))
        api = sync.notion()
        queue_scope = json.dumps([scope, sorted(config.get('playlist_ids', []))])
        snapshot = db.execute('SELECT fetched, value FROM candidate_snapshots WHERE scope=?', (queue_scope,)).fetchone()
        reuse = snapshot and time.time() - snapshot[0] < 86400
        found = {} if reuse else sync.playlist_databases(api, parent, defer_conflicts=True)
        candidates = json.loads(snapshot[1]) if reuse else []
        statuses = ['Pending', 'Disabled', 'Error', 'Blocked']
        filter_ = {'or': [{'property': 'Transcript status', 'rich_text': {'equals': s}} for s in statuses]}
        filter_['or'].append({'property': 'Transcript status', 'rich_text': {'is_empty': True}})
        for index, (playlist, database) in enumerate(found.items(), 1):
            if config.get('playlist_ids') and playlist not in config['playlist_ids']:
                continue
            if isinstance(database, Exception):
                sync.progress.log('Skipping conflicting playlist databases')
                continue
            sync.progress.log(f'Transcripts: collecting candidates from database {index}/{len(found)}')
            candidates.extend(sync.pages(api, database['data_sources'][0]['id'], filter_))
        # Stable order and a persistent video cache avoid repeated scrapes across playlists/runs.
        candidates.sort(key=lambda p: (sync.plain(p, 'Video ID'), p['id']))
        if not reuse:
            with db:
                db.execute('INSERT OR REPLACE INTO candidate_snapshots VALUES (?, ?, ?)',
                           (queue_scope, time.time(), json.dumps(candidates)))
        sync.progress.log(f'Transcripts: {len(candidates)} candidate pages; budget {max_attempts} requests (including retries)')
        for index, page in enumerate(candidates, 1):
            video = sync.plain(page, 'Video ID')
            if not video:
                continue
            row = db.execute('SELECT value FROM results WHERE scope=? AND video=?', (scope, video)).fetchone()
            result = json.loads(row[0]) if row else None
            now = time.time()
            if result and result['status'] == 'Blocked':
                result = None
            if result and result['status'] not in ('Full', 'No captions returned', 'No matching language', 'Video unavailable') and now - result['at'] >= cooldown:
                result = None
            if result is None:
                if totals['attempts'] >= max_attempts or (now < blocked_until and not wait_on_block):
                    continue
                status = sync.plain(page, 'Transcript status')
                if status == 'Error' and not sync.should_fetch(page, datetime.now(timezone.utc), {**config, 'transcripts': 'best-effort'}):
                    continue
                while totals['attempts'] < max_attempts:
                    wait_until(max(blocked_until, next_request), 'cooldown / request spacing')
                    sync.progress.log(f'Transcripts: fetch {totals["attempts"] + 1}/{max_attempts}, candidate {index}/{len(candidates)}')
                    text, status, language = sync.transcript(video, {**config, 'transcript_max_chars': 0})
                    totals['attempts'] += 1
                    result = dict(text=text, status=status, language=language, at=time.time())
                    if status == 'Blocked':
                        strikes = min(strikes + 1, 5)
                        blocked_until = time.time() + min(21600, 1800 * 2 ** (strikes - 1))
                    paced_delay = max(delay, min(300, delay * 2 ** strikes))
                    next_request = time.time() + paced_delay + random.uniform(0, paced_delay * .25)
                    with db:
                        db.execute('INSERT OR REPLACE INTO results VALUES (?, ?, ?)', (scope, video, json.dumps(result)))
                        db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (scope, blocked_until))
                        db.execute('INSERT OR REPLACE INTO throttle VALUES (?, ?, ?)', (scope, strikes, next_request))
                    sync.progress.log(f'Transcripts: {status}')
                    if status != 'Blocked' or not wait_on_block:
                        break
            else:
                totals['cached'] += 1
            delivered = db.execute('SELECT fetched FROM delivered WHERE scope=? AND page=?', (scope, page['id'])).fetchone()
            if delivered and delivered[0] == result['at']:
                continue
            # Fresh read protects completed captions, including results from another writer.
            try:
                current = api.call('GET', 'pages/' + page['id'])
                if sync.plain(current, 'Transcript status') in ('Full', 'Partial', 'No captions returned'):
                    continue
                checked = current.get('properties', {}).get('Transcript checked', {}).get('date')
                stamp = datetime.fromtimestamp(result['at'], timezone.utc).isoformat()
                if checked and checked.get('start') == stamp:
                    continue
                # Result stays in SQLite until all copies can be delivered, even after interruption.
                # Publish success only after all transcript blocks have been written.
                if result['status'] == 'Full':
                    sync.write_body(api, page['id'], 'Transcript\n\n' + result['text'])
                api.call('PATCH', 'pages/' + page['id'], json={'properties': {
                    'Transcript status': sync.rich(result['status']),
                    'Transcript language': sync.rich(result['language']),
                    'Transcript checked': sync.date(stamp)}})
                with db:
                    db.execute('INSERT OR REPLACE INTO delivered VALUES (?, ?, ?)', (scope, page['id'], result['at']))
                totals['saved_pages'] += 1
                sync.progress.log(f'Transcripts: page {index}/{len(candidates)} saved; {result["status"]}')
            except (sync.TemporaryAPIError, sync.NotionValidationError) as exc:
                totals['deferred_pages'] += 1
                sync.progress.log(f'Transcript save deferred: {exc}')
        sync.progress.log('Transcript batch finished; rerun to continue. Completed transcripts are retained.')
        print(json.dumps({'transcript_totals': totals}), flush=True)
        return totals
    finally:
        db.close()
