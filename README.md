# Gemba Wrapped

Scans a Slack channel for Spotify track links posted in a date range and adds
any new ones to a Spotify playlist.

## How it works

- `slack_to_spotify.py` — reads Slack messages, extracts Spotify track links,
  and adds any not already in the playlist. Dedup is done by ISRC (not raw
  Spotify track ID), since Spotify can return a different regional ID for the
  same recording depending on request context.
- `check_setup.py` — verifies your `.env` and `config.json` are set up
  correctly, and checks live Slack/Spotify connectivity, **before** you run
  the real sync. Run this first if anything's not working.

## Requirements

- Python 3.9+
- A Spotify Premium account (required for Development Mode apps)
- A Slack workspace where you can install a bot

## Setup

### 1. Create a virtual environment and install dependencies

```bash
cd gemba-wrapped
python3 -m venv .venv
source .venv/bin/activate      # run this every time you open a new terminal
pip install -r requirements.txt
```

> **macOS + LibreSSL warning:** if you see `NotOpenSSLWarning` when running
> the scripts, it's because the system Python links against LibreSSL instead
> of OpenSSL. `requirements.txt` pins `urllib3<2` to avoid this. For a more
> permanent fix, rebuild the venv using a Homebrew-installed Python instead
> of the system one.

### 2. Spotify app setup

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/create)
   and create an app.
2. When asked "Which API/SDKs are you planning to use?", select **Web API**.
3. Set the redirect URI to: `http://127.0.0.1:8080/callback`
4. Copy the **Client ID** and **Client Secret** into your `.env` (see below).
5. Find your playlist ID from its share link:
   `https://open.spotify.com/playlist/<spotify_playlist_id>?si=...`
6. **The playlist must be one you own or collaborate on.** Spotify's API only
   returns playlist contents for playlists you own or collaborate on — for
   any other playlist you'll only get metadata back (which will make the
   dedup check silently think the playlist is empty).

### 3. Slack app setup

1. Create a Slack app and install it to your workspace as a bot.
2. Grant it the `channels:history` (or `groups:history` for private channels)
   and `channels:read` scopes, and invite the bot to the target channel with
   `/invite @yourbotname`.
3. Copy the bot token (starts with `xoxb-`) into your `.env`.
4. Find the channel ID via the channel details panel in Slack
   (right-click channel → View channel details → copy Channel ID).

### 4. Configure the project

Copy the example files and fill them in:

```bash
cp .env.example .env
cp config.example.json config.json
```

**`.env`:**

| Variable | Required | Notes |
| --- | --- | --- |
| `SLACK_BOT_TOKEN` | Yes | Starts with `xoxb-` |
| `SPOTIFY_CLIENT_ID` | Yes | From the Spotify dashboard |
| `SPOTIFY_CLIENT_SECRET` | Yes | From the Spotify dashboard |
| `SPOTIFY_REDIRECT_URI` | No | Defaults to `http://127.0.0.1:8080/callback` |
| `SPOTIFY_CACHE_PATH` | No | Defaults to `.spotify_cache` in the current directory |

**`config.json`:**

| Field | Description |
| --- | --- |
| `slack_channel_id` | Channel to scan for links |
| `spotify_playlist_id` | Playlist to add tracks to |
| `start_date` | `YYYY-MM-DD`, inclusive |
| `end_date` | `YYYY-MM-DD`, inclusive |

### 5. Verify your setup

```bash
python check_setup.py
```

This checks your `.env` and `config.json` for missing/malformed values, and
if those look OK, tests that the Slack token can see the channel and the
Spotify credentials can access the playlist. Fix anything it flags before
moving on.

## Running

```bash
python slack_to_spotify.py            # scan and add new tracks
python slack_to_spotify.py --dry-run  # preview without modifying the playlist
```

The first run will open a browser window for Spotify OAuth; the resulting
token is cached to `.spotify_cache` (or `SPOTIFY_CACHE_PATH`) so you won't be
prompted again until it expires.

## Security note

`.spotify_cache` contains your OAuth tokens — it's listed in `.gitignore`
and should never be committed. If it's already been tracked, run:

```bash
git rm --cached .spotify_cache
git commit -m "Stop tracking .spotify_cache"
```

If it was ever pushed to a shared/remote repo, treat the tokens as
compromised and regenerate your Spotify app credentials.

## Troubleshooting

- **"Bot is NOT a member of #channel"** — run `/invite @yourbotname` in the
  target Slack channel.
- **"Playlist is owned by 'X' and not marked collaborative"** — you can only
  reliably add to playlists you own or that are marked collaborative.
- **Duplicates keep getting added** — make sure you're on the latest version
  of `slack_to_spotify.py`. Spotify's February 2026 API migration renamed
  fields and endpoints in ways that silently broke both the ISRC-based dedup
  and playlist-read logic in earlier versions of this script.
- **`NotOpenSSLWarning`** — see the note under Setup step 1.