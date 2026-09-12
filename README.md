# Private YouTube playlists → Notion

A runnable Python importer and GitHub Actions workflow. Account setup is required;
this package does not contain credentials and has not been live-tested against your accounts.

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
Name, Channel, Description, Published and Added. They sort by Added, newest first,
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

The destination page has been created:
[Videos](https://www.notion.so/3d89fefea08881d6823cdf66dfae098b).
Its ID is already in config.json as `notion_parent_page_id`.

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

- Every run scans all selected playlist pages, including additions placed in the
  middle/end, and refreshes current video metadata. Unchanged rows are not rewritten.
- Removals are marked only after a complete successful scan of that playlist.
  Entire deleted/inaccessible playlists are retained; they are not inferred deleted.
- Removed rows and unavailable-video records are retained. This is an archive,
  not an automatic erasure tool. Re-adding a video creates a new playlist-item ID
  and a new historical row.
- Notion itself is the sync state; no GitHub cache is required. An interrupted
  block write leaves the completion marker unset so a subsequent run retries.
  The managed transcript toggle may be incomplete until recovery succeeds.
- Ambiguous writes are not automatically retried within a run. If an unexpected
  duplicate Item ID exists, the next run stops for manual reconciliation.
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
