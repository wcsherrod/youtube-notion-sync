# Private YouTube playlists → Notion

A runnable Python importer and GitHub Actions workflow. Account setup is required;
this package does not contain credentials. Local imports have been exercised against the configured accounts; automated tests use mocked APIs.

## What it saves

A separate database for each playlist, nested under the Videos page.
Each database contains one row per playlist item. The same video in two playlists appears
in both groups; duplicate occurrences within a playlist are also preserved. YouTube
playlist-item IDs are the stable keys, so changing a playlist name does not create duplicates.

| Field | Meaning |
| --- | --- |
| Name | Video title |
| Playlist / Playlist ID | Playlist name and stable identifier |
| Channel | Video uploader, not playlist owner |
| Published | Video publication date |
| Added | Date added to this playlist |
| Thumbnail | Best available thumbnail URL, also used as the page cover |
| Description | Full available YouTube description |
| Video URL / Video ID | Original video link and ID |
| Playlist URL / Position | Original playlist link and current position |
| In playlist | False after removal; records are retained |
| Transcript status / language / checked | Full, Partial, Pending, Blocked, Unavailable, Error, or Disabled |

Transcripts are timestamped text inside a dedicated toggle in the page body. Add
personal notes outside that toggle; the importer leaves those notes intact.
Descriptions stay in the Description property. Thumbnail URLs are references,
not archived image files, so they may stop working if the source disappears.
Deleted/private videos belonging to someone else may have incomplete metadata.

Every playlist database automatically receives a **Video cards** gallery as its
first view tab. Cards use medium-size, uncropped page-cover thumbnails and show
Name, OPEN VIDEO, Channel, Description, Published and Added. OPEN VIDEO displays the bold blue link ↗ VIEW IN BROWSER ↗. They sort by Added, newest first,
and show only entries still in the playlist. Notion controls text clipping.

The same layout is used for every newly discovered playlist. Existing Video cards
galleries are reused; personal edits to those views are preserved. A missing gallery
is created on the next run, including recovery after interrupted database creation.
Other view tabs are retained. Views use Notion API version 2026-03-11, while the
existing metadata API calls retain their current version.

Database names track playlist names. Databases are matched using a stable playlist
ID stored in their description; leave that description intact. Equal playlist names
still produce separate databases. Gallery API calls have mocked tests but need live
verification when credentials are configured and the first playlist is imported.

## 1. Prepare Python locally

Use Python 3.11 or newer on your computer. Extract this folder and open a terminal in it.

```sh
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 2. Authorize your YouTube account once

1. Create a Google Cloud project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen for personal use. While in Testing, add
   your Google account as a test user. Google gives these refresh tokens a
   seven-day lifetime; for ongoing operation, configure the app's publishing
   status appropriately (Production) and authorize again. Production status does
   not itself guarantee verification or prevent future token revocation.
3. Create an OAuth client of type **Desktop app**. Download its JSON and save it
   here as `client_secret.json`.
4. Run `python sync.py auth` on your computer, then sign in with the account/channel
   that owns the playlists. This requests read-only YouTube access.
5. The command saves `youtube-token.json`. Do not commit it or paste it into chat.

Load that file into the environment for local commands:

PowerShell:
```powershell
$env:YOUTUBE_TOKEN_JSON = Get-Content -Raw youtube-token.json
```

macOS/Linux:
```sh
export YOUTUBE_TOKEN_JSON="$(cat youtube-token.json)"
```

List your playlists:
```sh
python sync.py playlists
```

Copy the chosen playlist IDs into `config.json`. An empty `playlist_ids` array
means **all playlists owned by the authorized channel**, including private ones.
Use explicit IDs if you only want selected lists. Owned standard playlists are
supported; Watch Later and Watch History are not exposed through this API.
Saved playlists owned by other channels are outside this initial implementation.

## 3. Connect the Videos page

Create a private Notion page named Videos and set its ID in config.json as
`notion_parent_page_id`, or supply the NOTION_PARENT_PAGE_ID environment variable.

Create an internal Notion integration with read, insert and update permissions.
Connect it to **Videos** and set its token as `NOTION_TOKEN` in your terminal
(use your secret manager or a private shell session). This is separate from
ChatGPT's Notion connection. You may override the destination with the environment
variable `NOTION_PARENT_PAGE_ID`.

```sh
python sync.py sync
```

The first authorized run discovers playlists, creates one child database per
playlist, and imports the videos. Subsequent runs reuse those databases. New
playlists get their own databases automatically when playlist_ids is empty.
Check a small playlist before enabling the schedule. No `init` command is needed.

## 4. Schedule with GitHub Actions

Create a private GitHub repository and put this folder's **contents** at its root,
including `.github/workflows/sync.yml`. Never upload the OAuth JSON files. If using
the GitHub website, ensure you upload the workflow as well as the visible files.

In repository Settings → Secrets and variables → Actions, add:

| Secret | Value |
| --- | --- |
| `YOUTUBE_TOKEN_JSON` | Complete contents of `youtube-token.json` |
| `NOTION_TOKEN` | Internal integration token |
| `NOTION_PARENT_PAGE_ID` | Optional: override the Videos page ID in config.json |

Enable Actions, commit the workflow to the default branch, then use Actions →
Sync YouTube playlists to Notion → Run workflow for the first check. It is configured
for 00:23, 06:23, 12:23 and 18:23 UTC daily. Scheduled runs can be delayed or dropped
by GitHub; this is polling, not an immediate notification system. Check your account's
Actions allowance and failed-run notifications.

Only one GitHub run is allowed at a time. Do not run the local importer against
the same database concurrently: Notion does not enforce uniqueness on Item ID.
No access tokens, private titles or transcript artifacts are printed by scheduled
syncs. Logs contain aggregate counts or a sanitized failure message.

## Transcripts

YouTube's official captions-download endpoint requires permission to edit the
video. Owning a playlist is not enough. This tool instead optionally uses
`youtube-transcript-api` for captions available without signing into the video.
A private playlist containing public videos is supported; this does not grant
transcript access to someone else's private or restricted videos.

The library documents frequent blocking of cloud-provider IPs. Expect GitHub-hosted
transcript requests to fail for some or many videos. Metadata imports continue;
the importer stops further transcript attempts for that playlist after an IP block.
No paid proxy or transcription service is configured.

For better odds, run the same sync locally on a home computer, or move the workflow
to a self-hosted runner. Do not run both simultaneously. Set `transcripts` to `off`
for metadata-only GitHub runs, and use a separate local config with `best-effort`
for transcript backfill. It remains best-effort on a home connection too.

Configuration:

- `languages`: ordered language preferences, initially English only.
- `transcript_max_chars`: `0` saves all fetched text; a positive value saves a
  labeled partial transcript of that character length.
- `transcript_budget`: at most 50 new fetch attempts per playlist per run by default.
- `transcript_retry_days`: retry missing/blocked/error results after seven days.

Successful Full/Partial transcripts are retained without refreshing. To retry one
immediately or replace a partial copy with a full copy, clear that row's Transcript
status and Transcript checked values, adjust config, and rerun. Caption corrections
on YouTube are not automatically refreshed in this initial version.

## Searching from ChatGPT

Connect Videos and its child databases to your ChatGPT Notion connection as well as the importer
integration. The two permissions are separate. Your Notion connection currently reports keyword search available, while AI search
requires another plan. Keyword search is the intended route here; verify it using a distinctive
phrase found only inside one imported transcript. Check that ChatGPT finds the
correct page and can quote the matching passage before relying on it at scale.

Notion's REST `/search` is title-only. Merely adding transcripts to pages does not
prove that a particular ChatGPT connector indexes them. This project includes an
exhaustive fallback that reads the database and recursively searches page blocks:

```sh
python sync.py search --query "phrase from a transcript"
```

It prints matching titles, playlist names, Notion links and excerpts. This fallback
is a local CLI, not an installed ChatGPT tool; it can be slow on a large archive.
A separate searchable index/custom connector would be needed if the available
Notion connection cannot search the imported content reliably. That service is
not included in this package.

## Recovery and limits

- Each new pass scans all selected playlist pages. Interrupted passes resume only unfinished playlists. Unchanged entries within a playlist are not rewritten.
- Removals are marked only after a complete successful scan of that playlist.
  Entire deleted/inaccessible playlists are retained; they are not inferred deleted.
- Removed rows and unavailable-video records are retained. This is an archive,
  not an automatic erasure tool. Re-adding a video creates a new playlist-item ID
  and a new historical row.
- Notion stores completed video records. A playlist checkpoint records progress through the current pass. Incomplete managed transcript bodies are retried later.
- Safe requests retry transient network errors and server failures up to eight attempts. Uncertain page creations are reconciled by Item ID; uncertain block writes are deferred. Duplicate rows are preserved, and the most complete copy is selected for syncing.
- OAuth revocation requires reauthorizing and replacing the GitHub secret.
- Large initial imports may exceed a run's three-hour timeout; rerun to continue.
  Large libraries also require checking YouTube quota and Notion storage/API limits.
- Dependency versions have major-version bounds; this is not a fully locked build.

## Validation

`python -m unittest discover -s tests` exercises long Unicode content, dates,
retry eligibility, pagination, preservation of user notes, no-op repeated sync,
and failure-safe removal handling using mocked APIs. Live account authorization,
Notion creation and GitHub execution must still be tested during setup.

## Primary documentation

- [YouTube playlists](https://developers.google.com/youtube/v3/docs/playlists/list)
- [Playlist items and access restrictions](https://developers.google.com/youtube/v3/docs/playlistItems/list)
- [Caption download permissions](https://developers.google.com/youtube/v3/docs/captions/download)
- [Google refresh-token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- [Transcript library and cloud blocking](https://github.com/jdepoix/youtube-transcript-api)
- [Notion database creation](https://developers.notion.com/reference/create-a-database)
- [Notion title search](https://developers.notion.com/reference/post-search)
- [GitHub schedule events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

The former single-database version is superseded. This version does not automatically
migrate records from an earlier single-database import. No such import was run here.

## Transcript diagnostics

The Actions job summary reports scanned entries, saved transcripts, pending entries,
and unsuccessful entries, with counts by status. These count playlist entries, not
unique videos. Pending means not attempted yet, not a confirmed failure.

- Full / Partial: retrieved caption text.
- Blocked: request or IP rejected; does not establish a permanent video restriction.
- No captions returned: the library reported TranscriptsDisabled. This may mean
  absent or disabled captions; it does not prove the video is instrumental.
- No matching language: no track matched the configured languages.
- Video unavailable: the video could not be accessed by the scraper.
- Error: another retrieval error; do not interpret as absent captions.

Unsuccessful retrievals remain eligible after transcript_retry_days (seven days by
default). Saved transcripts are retained. Metadata still imports after caption
failures. The importer does not classify music or speech from audio.

## Privacy

See the [Privacy Policy](PRIVACY.md) for how this importer accesses, uses and stores data.

## Terms

See the [Terms of Service](TOS.md).



## Checkpoints and resumed runs

No checkpoint means a full scan from playlist 1, regardless of existing Notion databases.
After each playlist completes without deferred writes, the checkpoint is saved. Fatal errors,
quota exhaustion, Ctrl+C, or process termination leave the last successful checkpoint in place.
A later run skips its completed playlist IDs, including when YouTube changes the listing order.
Newly discovered playlists are appended; missing playlists are not treated as deleted Notion data.

Checkpoint completion means the metadata and any attempted caption writes finished. It does
not mean every video has a transcript. Caption limits, blocked requests and retry cooldowns
still apply. A playlist with deferred writes remains unfinished while other playlists proceed.
The next run retries those unfinished playlists. Counts printed during a resumed run describe
that run's processed entries, not all records accumulated in Notion.

After the entire pass completes, the checkpoint is cleared and the next run scans all playlists
again for additions/removals and eligible caption backfill. Additions to an already completed
playlist during a long interrupted pass are picked up by that next full scan.

Local runs use `.youtube-sync-checkpoint.json` in the working directory. Keep running from the
same project folder. Writes use a temporary file, flush/fsync, then atomic replacement. Rename
or remove the checkpoint to deliberately restart a full scan. Checkpoints bind to the destination,
OAuth identity, and sync settings; an account/settings/version change starts a fresh full scan.
The previous version-1 checkpoint is intentionally restarted once because it was not bound to
account/settings. Malformed checkpoints fail explicitly instead of silently skipping records.

GitHub Actions defaults to a managed **YouTube sync checkpoint (managed)** page under Videos.
It stores private snapshots after each completed playlist and retains only the last two. This
survives runner teardown, timeout and new workflow runs; no public artifact/cache contains the
private playlist IDs. Completing a pass writes an empty-state snapshot so the next run starts
fresh. This remote persistence path is covered by mocks; live Actions verification is still needed.

Optional config: `checkpoint_storage` is `local` or `notion`; `checkpoint_file` changes the local
filename. Local and GitHub defaults are separate checkpoints and do not provide a distributed
lock. Do not run both importers concurrently. A local checkpoint is not automatically transferred
to GitHub, so its first run performs its own full scan.

Local runs now save a SQLite snapshot for each unfinished playlist next to the JSON checkpoint.
A complete playlist-item listing is saved first, followed by each metadata batch. Each video is
marked complete only after its final Notion write succeeds (or saved content is unchanged).
Restarting reuses those inputs, skips completed videos, and retries unfinished saves. Initial
playlist pagination must finish before its listing is cached; interruption during that stage
restarts that listing. Metadata batches already cached do not need to be downloaded again.

Keep the JSON checkpoint and its adjacent SQLite files together. SQLite snapshots contain private
playlist metadata; they are ignored by git and removed after their playlist is checkpointed complete.
Deleting the JSON checkpoint starts a new full pass and invalidates old snapshots. Existing version-2
checkpoints gain a pass identifier automatically without discarding completed playlists.
Changes during an interrupted snapshot are picked up on the next fresh full scan. Removal
reconciliation is limited to the Notion entries present when that snapshot was created.

GitHub Actions still resumes at playlist boundaries through its private Notion checkpoint;
these local per-video snapshots are not transferred between hosted runners. YouTube playlists
are listed once per run and the authenticated client refreshes credentials as needed.

## Diagnostics and progress

Every video prints a saved/skipped status. A 20-second heartbeat reports the process is alive,
not that a network request is advancing. Retry waits and deferred writes are reported separately.
Known API error codes are printed without raw error bodies or tokens. `python sync.py diagnose`
is a read-only YouTube probe of playlist 2; `--playlist-index N` selects a different playlist.

Requests now announce each active attempt separately from retry waits. API and OAuth HTTP
transports use a 10-second connection timeout and 30-second read-inactivity timeout. These are
not absolute wall-clock deadlines: DNS, multiple addresses and Google's internal refresh retries
can add time. Retry-After delays over two minutes are deferred. Playlist-specific not-found/access
errors and exhausted transient retries leave that playlist pending and continue to others;
quota exhaustion and credential/configuration errors still stop the run. Existing Notion records
are preserved when playlist access fails. A heartbeat means process activity, not import progress.

## Long-run safety review

Caption HTTP requests now also have connection/read-inactivity timeouts. Local snapshots retain
fetched captions awaiting a successful Notion save, avoiding another scrape after an interrupted
write. Existing completion hashes are cleared before updates and restored only after body writes
finish. Transient database/gallery setup failures also defer that playlist and continue the pass.

When resuming, playlists completed at least six hours earlier become eligible for a fresh scan.
They are placed after unfinished playlists so repeated scheduled timeouts do not starve the initial
import. Their old per-video snapshots are invalidated. This prevents one permanently unavailable
playlist from indefinitely suppressing updates to completed playlists. Old checkpoints without
completion timestamps start this clock on their first run with this version.

The command performs one pass and exits; it is not a continuously polling daemon. GitHub schedules
subsequent runs, each with a 180-minute limit. Quota exhaustion stops the run without clearing its
checkpoint and does not automatically sleep until quota resets. Invalid credentials, permissions,
configuration, filesystem failures, and unhandled API errors can also stop it. Read timeouts are
inactivity limits, not a guarantee against every possible operating-system/network stall.

Tests exercise mocked failures and recovery; they do not prove unlimited unattended uptime or
complete transcript coverage. Local per-video state remains local; Actions retains playlist-level
state. Source changes during an unfinished cached playlist are reconciled on a later fresh scan.


### Notion validation failures

An individual video's HTTP 400 `validation_error` now logs Notion's detailed message together with the video and playlist-item IDs, then defers that video and continues. Known Notion credentials are redacted; validation details can still contain video metadata, so review logs before sharing them publicly. Authentication and database setup failures still stop the run.

With local checkpoints, deferred errors and fetched captions remain in the existing SQLite cache. Failed videos never receive a completion marker, and their playlist remains incomplete. Run the normal sync command again after correcting the reported problem; completed videos are skipped and cached captions are reused. Do not delete checkpoint files. If a page was already created before a later write failed, the next run finds it by Item ID and updates it.

Text chunks now stay within 1,800 UTF-16 units as well as Python characters, preserving complete descriptions and transcripts including emoji. This prevents a potential text-length validation failure; other validation failures require the specific message to diagnose. This update does not claim to identify the cause of any previously hidden error.
