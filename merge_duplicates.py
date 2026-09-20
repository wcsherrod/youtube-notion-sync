"""Read-only merge planning and resumable, non-destructive consolidation."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from datetime import datetime, timezone
import sync

BUILD = '2026-09-20-merge-recovery-v2'
PLAN_BUILDS = {'2026-09-19-merge-v1', BUILD}
BACKUP_PREFIX = 'youtube-notion-sync merge backup: '


def norm(value):
    return value.replace('-', '')


def title(db):
    return ''.join(t.get('plain_text', t.get('text', {}).get('content', '')) for t in db.get('title', []))


def marker(db):
    return ''.join(t.get('plain_text', t.get('text', {}).get('content', '')) for t in db.get('description', []))


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)')
    def get(self, key):
        row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None
    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))
    def close(self):
        self.db.close()


def writable(page):
    result = {}
    for name, prop in page['properties'].items():
        kind = prop['type']
        if kind in ('formula', 'created_time', 'last_edited_time', 'created_by', 'last_edited_by'):
            continue
        if kind in ('rich_text', 'title'):
            values = prop[kind]
            if len(values) >= 25 or any(v.get('type', 'text') != 'text' for v in values):
                raise sync.SyncError('Long or non-text custom property needs manual review: ' + name)
            result[name] = {kind: [dict(type='text', text=v['text'],
                **({'annotations':v['annotations']} if 'annotations' in v else {})) for v in values]}
        elif kind in ('url', 'number', 'checkbox', 'date'):
            result[name] = {kind: prop[kind]}
        else:
            raise sync.SyncError(f'Unsupported custom property {name}: {kind}; pair needs manual review')
    return result


def rank(page):
    return ({'Full': 3, 'Partial': 2}.get(sync.plain(page, 'Transcript status'), 0),
            bool(sync.plain(page, 'Content hash')), len(sync.plain(page, 'Description')), page['id'])


def decide(databases, rows):
    # Deterministic destination; all decisions are persisted before any move.
    target = max(databases, key=lambda d: (len({sync.plain(p, 'Item ID') for p in rows[d['id']]}), d['id']))
    grouped = {}
    for db in databases:
        for page in rows[db['id']]:
            item = sync.plain(page, 'Item ID')
            if not item:
                raise sync.SyncError('A row has no Item ID; leave this pair for manual review')
            grouped.setdefault(item, []).append((db, page))
    winners, retained = [], []
    backups = [d for d in databases if d['id'] != target['id']]
    for item, copies in sorted(grouped.items()):
        db, page = max(copies, key=lambda pair: rank(pair[1]))
        props = writable(page)
        description_page = max((p for _, p in copies), key=lambda p: len(sync.plain(p, 'Description')))
        if 'Description' in description_page['properties']:
            props['Description'] = writable(description_page)['Description']
        props['Content hash'] = sync.rich('')  # force normal sync to re-evaluate metadata
        others = [p for _, p in copies if p['id'] != page['id']]
        links = [{'type':'text','text':{'content':'Preserved copy ' + str(i+1),
                  'link':{'url':'https://www.notion.so/' + norm(p['id'])}}} for i,p in enumerate(others)]
        if len(links) > 100:
            raise sync.SyncError('More than 100 copies of one item; manual review required')
        props['Merge copies'] = {'rich_text':links}
        winners.append({'page':page, 'source':db['data_sources'][0]['id'], 'props':props, 'item':item})
        for other_db, other in copies:
            if other['id'] == page['id']:
                continue
            # Remove overlapping rows from active database by moving to a backup.
            destination = backups[0] if other_db['id'] == target['id'] else other_db
            retained.append({'page':other, 'source':other_db['data_sources'][0]['id'],
                             'props':writable(other), 'destination':destination['data_sources'][0]['id']})
    return target, winners, retained


def plan(api, parent, journal, report):
    if journal.get('plan'):
        raise sync.SyncError('Plan already exists. Use apply to resume, or choose a new journal filename.')
    grouped = {}
    for block in sync.children(api, parent):
        if block['type'] != 'child_database':
            continue
        db = api.call('GET', 'databases/' + block['id'])
        tag = marker(db)
        if tag.startswith(sync.DB_PREFIX):
            grouped.setdefault(tag[len(sync.DB_PREFIX):], {})[db['id']] = db
    pairs, lines = [], ['# Duplicate playlist merge report', '',
        'Read-only plan. Originals are preserved in backup databases; nothing is archived or deleted.', '']
    for playlist, values in grouped.items():
        databases = list(values.values())
        if len(databases) < 2:
            continue
        sync.progress.log('Comparing duplicate playlist ' + playlist)
        if any(len(d.get('data_sources', [])) != 1 for d in databases):
            raise sync.SyncError('Multiple data sources in a duplicate database; manual review required')
        rows, schemas = {}, []
        for db in databases:
            ds = db['data_sources'][0]['id']
            schema = api.call('GET', 'data_sources/' + ds)['properties']
            schemas.append({name: prop['type'] for name,prop in schema.items()})
            rows[db['id']] = list(sync.pages(api, ds))
            for p in rows[db['id']]:
                if sync.plain(p, 'Playlist ID') != playlist:
                    raise sync.SyncError('Playlist ID mismatch in a row; manual review required')
        if any(s != schemas[0] for s in schemas[1:]):
            raise sync.SyncError('Duplicate database schemas differ; manual review required before moving pages')
        target, winners, retained = decide(databases, rows)
        pair = {'playlist':playlist, 'databases':databases, 'target':target,
                'winners':winners, 'retained':retained, 'rows':rows, 'schema':schemas[0]}
        journal.set('pair:' + playlist, pair)
        pairs.append(playlist)
        lines += ['## ' + title(target), '', 'Playlist: ' + playlist,
                  'Destination: https://www.notion.so/' + norm(target['id']),
                  f'Unique items: {len(winners)}; extra copies retained: {len(retained)}', '']
        for db in databases:
            full = sum(sync.plain(p, 'Transcript status') == 'Full' for p in rows[db['id']])
            lines.append(f'- {len(rows[db["id"]])} rows, {full} Full transcripts: https://www.notion.so/{norm(db["id"])}')
        lines.append('')
    Path(report).write_text('\n'.join(lines), encoding='utf-8')
    journal.set('plan', {'build':BUILD,'parent':parent,'playlists':pairs,
                       'created':datetime.now(timezone.utc).isoformat()})
    sync.progress.log(f'Plan saved: {len(pairs)} duplicate playlists. Report: {report}')


def recover(operation, label):
    failures = 0
    while True:
        try:
            return operation()
        except sync.TemporaryAPIError as exc:
            failures += 1
            delay = min(300, 15 * 2 ** min(failures - 1, 5))
            sync.progress.log(f'{label}: {exc}; recovery {failures}, pause {delay}s, then reconcile saved state')
            # Keep Ctrl+C responsive; no secrets or raw request payloads in logs.
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline - time.monotonic())))


def move_record(api, journal, record, destination):
    return recover(lambda: move_record_once(api, journal, record, destination),
                   'Page ' + record['page']['id'])


def move_record_once(api, journal, record, destination):
    page = record['page']; pid = page['id']; key = 'page:' + pid
    if journal.get(key + ':done'):
        return
    current = api.call('GET', 'pages/' + pid)
    actual = current.get('parent', {}).get('data_source_id', '')
    if norm(actual) not in (norm(record['source']), norm(destination)):
        raise sync.SyncError('Page moved outside planned databases; stop and review ' + pid)
    if current.get('in_trash') or current.get('archived'):
        raise sync.SyncError('A planned page was archived externally; stop and review ' + pid)
    started = journal.get(key + ':started')
    if not started:
        if current.get('last_edited_time') != page.get('last_edited_time'):
            raise sync.SyncError('Page changed since comparison; create a fresh plan before merging ' + pid)
        # Preserve complete original page properties and top-level block identities.
        started = {'original':current,'blocks':[b['id'] for b in sync.children(api,pid)]}
        journal.set(key + ':started', started)
    if norm(actual) != norm(destination):
        # No blind retry after an ambiguous POST. A later run checks the parent first.
        api.call('POST', 'pages/' + pid + '/move', json={
            'parent':{'type':'data_source_id','data_source_id':destination}})
    api.call('PATCH', 'pages/' + pid, json={'properties':record['props']})
    check = api.call('GET', 'pages/' + pid)
    if norm(check.get('parent',{}).get('data_source_id','')) != norm(destination):
        raise sync.SyncError('Move verification failed for ' + pid)
    expected = record['props']
    actual_props = writable(check)
    for name, prop in expected.items():
        if 'rich_text' in prop or 'title' in prop:
            expected_text = sync.plain({'properties':{name:prop}}, name)
            if sync.plain(check,name) != expected_text:
                raise sync.SyncError('Text verification failed for ' + pid + ': ' + name)
        elif actual_props.get(name) != prop:
            raise sync.SyncError('Property verification failed for ' + pid + ': ' + name)
    if [b['id'] for b in sync.children(api,pid)] != started['blocks']:
        raise sync.SyncError('Page content changed during move; stop and review ' + pid)
    journal.set(key + ':done', True)
    sync.progress.log('Verified merged/preserved page ' + pid)


def reset_local_checkpoint(config, parent, playlist, path=None):
    path = path if path is not None else sync.checkpoint_path(config)
    if not path.exists():
        return
    state = json.loads(path.read_text(encoding='utf-8'))
    sync.validate_checkpoint(state)
    if norm(state['parent_page_id']) != norm(parent):
        raise sync.SyncError('Local checkpoint belongs to another parent')
    state['completed_playlist_ids'] = [p for p in state['completed_playlist_ids'] if p != playlist]
    generations = state.setdefault('playlist_generations', {})
    generations[playlist] = generations.get(playlist, 0) + 1
    sync.save_checkpoint(path, state)


def apply(api, parent, config, journal):
    saved = journal.get('plan')
    if not saved or saved['build'] not in PLAN_BUILDS or norm(saved['parent']) != norm(parent):
        raise sync.SyncError('Missing or incompatible merge plan')
    for playlist in saved['playlists']:
        if journal.get('finished:' + playlist):
            continue
        pair = journal.get('pair:' + playlist)
        target = pair['target']; ds = target['data_sources'][0]['id']
        # Reject externally added/removed pages before proceeding or resuming.
        expected_ids = {p['id'] for rows in pair['rows'].values() for p in rows}
        current_ids = set()
        for db in pair['databases']:
            current_db = api.call('GET','databases/'+db['id'])
            if current_db.get('parent') != db.get('parent'):
                raise sync.SyncError('Database parent changed since plan')
            allowed = (sync.DB_PREFIX + playlist, BACKUP_PREFIX + playlist + '; active database: ' + target['id'])
            if marker(current_db) not in allowed:
                raise sync.SyncError('Database marker changed since plan')
            schema = api.call('GET','data_sources/'+db['data_sources'][0]['id'])['properties']
            types = {k:v['type'] for k,v in schema.items()}
            expected_schema = dict(pair['schema'])
            if 'Merge copies' in types and 'Merge copies' not in expected_schema:
                expected_schema['Merge copies'] = 'rich_text'
            if types != expected_schema:
                raise sync.SyncError('Database schema changed since plan')
            current_ids.update(p['id'] for p in sync.pages(api,db['data_sources'][0]['id']))
        if expected_ids != current_ids:
            raise sync.SyncError('Database membership changed since plan; stop and review ' + playlist)
        api.call('PATCH','data_sources/'+ds,json={'properties':{'Merge copies':{'rich_text':{}}}})
        for record in pair['retained']:
            move_record(api,journal,record,record['destination'])
        for record in pair['winners']:
            move_record(api,journal,record,ds)
        final = list(sync.pages(api,ds))
        if {p['id'] for p in final} != {r['page']['id'] for r in pair['winners']}:
            raise sync.SyncError('Destination membership verification failed')
        # Invalidate item completion caches BEFORE removing conflict markers.
        # Repeating this after interruption is harmless and only triggers a fresh scan.
        reset_local_checkpoint(config,parent,playlist)
        if any(b['type'] == 'child_page' and b['child_page']['title'] == sync.NotionCheckpoint.TITLE
               for b in sync.children(api,parent)):
            reset_local_checkpoint(config,parent,playlist,sync.NotionCheckpoint(api,parent))
        for db in pair['databases']:
            if db['id'] != target['id']:
                api.call('PATCH','databases/'+db['id'],json={
                    'title':sync.rt('[Merge backup] ' + title(db)),
                    'description':sync.rt(BACKUP_PREFIX + playlist + '; active database: ' + target['id'])})
        journal.set('finished:' + playlist, True)
        sync.progress.log('Consolidated ' + playlist + '; backup copies preserved')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['plan','apply'])
    parser.add_argument('--config',default='config.json')
    parser.add_argument('--journal',default='youtube-merge.sqlite')
    parser.add_argument('--report',default='youtube-merge-report.md')
    parser.add_argument('--imports-stopped',action='store_true',help='Confirm local importer and GitHub Action are stopped')
    args=parser.parse_args()
    print('YouTube Notion merge build ' + BUILD, flush=True)
    if args.command=='apply' and not args.imports_stopped:
        parser.error('Stop the importer and GitHub Action, then use --imports-stopped')
    if Path('.env').exists():
        for line in Path('.env').read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key,value=line.split('=',1)
                os.environ.setdefault(key.strip(),value.strip().strip('\"\''))
    config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    parent=os.environ.get('NOTION_PARENT_PAGE_ID') or config['notion_parent_page_id']
    api=sync.notion(); api.headers['Notion-Version']='2026-03-11'
    lock=Path(args.journal+'.lock')
    try:
        fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:
        raise sync.SyncError('Merge lock exists; check for another merger before removing ' + str(lock))
    os.close(fd)
    journal=None
    try:
        journal=Journal(args.journal)
        with sync.progress:
            if args.command=='plan':plan(api,parent,journal,args.report)
            else:recover(lambda: apply(api,parent,config,journal), 'Merge')
    finally:
        if journal:journal.close()
        lock.unlink(missing_ok=True)


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:sys.exit('Stopped; journal preserved. Run the same command to resume apply.')
    except Exception as exc:sys.exit(sync.error_message(exc))
