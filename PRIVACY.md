# YouTube Notion Sync — Privacy Policy

Effective date: September 12, 2026

## Purpose

YouTube Notion Sync is a personal-use tool that copies an authorized user's YouTube playlist information and available captions into that user's chosen Notion workspace.

## Information accessed and used

With the user's permission, the tool requests read-only YouTube access to discover playlists, including private playlists, and retrieve playlist names, video identifiers, titles, channel names, descriptions, thumbnail links, publication dates, playlist addition dates and positions. Available captions may also be retrieved. This information is used to create and update the user's Notion video library. The tool does not modify the user's YouTube playlists or upload videos.

## Storage and service providers

Imported information is stored in the Notion workspace selected by the user. When scheduled through GitHub Actions, GitHub processes the information while running the importer. Authorization credentials are stored as GitHub Actions secrets or in local credential files when the tool is run locally. Google/YouTube, GitHub and Notion process information under their respective terms and privacy policies.

The source repository contains the importer code and documentation. Imported playlist contents, transcripts and authorization credentials are not intended to be committed to that repository. The importer is designed to report aggregate counts and error categories in scheduled-run logs rather than private titles or transcript text.

## Sharing and other uses

The tool transfers information to Notion and uses GitHub for scheduled execution only to provide its requested functionality. It does not sell personal information, use it for advertising, or send it to an AI model for training or transcription. Any separate AI connection the user grants to their Notion workspace is governed by that connection's settings and provider policies.

## Retention and user control

Imported Notion records remain until the user deletes them. Removing a video from a YouTube playlist marks its imported record as no longer in that playlist; it does not automatically erase the record. The user can stop scheduled execution in GitHub, delete stored credentials, and revoke Google account access at https://myaccount.google.com/connections. Revoking access prevents future authorized retrieval but does not delete previously imported Notion records. Copies and backups retained by service providers remain subject to their retention policies.

## Changes and contact

This policy will be updated if the tool's data practices change. Questions can be directed to the maintainer through the contact options available on https://github.com/wcsherrod. Do not include tokens or private playlist contents in public communications.
