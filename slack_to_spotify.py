#!/usr/bin/env python3
"""
slack_to_spotify.py

Scans a Slack channel for Spotify track links posted within a date range
and adds any not already in the playlist to a target Spotify playlist.

Dedup is done by ISRC (the recording's universal code), not by Spotify
track ID, because Spotify can "relink" a track to a different regional
ID depending on the market context of the request. Comparing raw IDs
between a shared link and a playlist_items() response can therefore
miss real duplicates.

Setup:
    pip install slack_sdk spotipy python-dotenv

    1. Copy .env.example to .env and fill in your credentials.
    2. Copy config.example.json to config.json and fill in your settings.
    3. Run:  python slack_to_spotify.py
       (add --dry-run to preview without modifying the playlist)
"""

import os
import re
import sys
import json
import argparse
from datetime import datetime, timezone

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import spotipy
from spotipy.oauth2 import SpotifyOAuth

TRACK_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]{22})"
)
TRACK_URI_RE = re.compile(r"spotify:track:([A-Za-z0-9]{22})")


def load_config(config_path: str) -> dict:
    with open(config_path, "r") as f:
        config = json.load(f)

    required = ["slack_channel_id", "spotify_playlist_id", "start_date", "end_date"]
    missing = [k for k in required if not config.get(k)]
    if missing:
        sys.exit(f"config.json is missing required field(s): {', '.join(missing)}")

    return config


def parse_date_to_ts(date_str: str, end_of_day: bool = False) -> float:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.timestamp()


def _extract_track_ids(msg, track_ids, seen):
    """Scan a single message's text/attachments for Spotify track links and
    append any new ones to track_ids (order-preserved, deduped via seen)."""
    text = msg.get("text", "")
    attachment_texts = " ".join(
        a.get("title", "") + " " + a.get("from_url", "")
        for a in msg.get("attachments", [])
    )
    full_text = text + " " + attachment_texts

    for pattern in (TRACK_URL_RE, TRACK_URI_RE):
        for match in pattern.finditer(full_text):
            tid = match.group(1)
            if tid not in seen:
                seen.add(tid)
                track_ids.append(tid)


def fetch_thread_replies(client, channel_id, thread_ts, oldest, latest, track_ids, seen):
    """Fetch every reply in a thread and scan each for track links. The
    parent message (first item returned) is skipped since it was already
    scanned as part of the main channel history."""
    cursor = None
    while True:
        try:
            resp = client.conversations_replies(
                channel=channel_id,
                ts=thread_ts,
                limit=200,
                cursor=cursor,
            )
        except SlackApiError as e:
            print(f"  Warning: could not fetch replies for thread {thread_ts}: {e.response['error']}")
            return

        for reply in resp.get("messages", []):
            if reply.get("ts") == thread_ts:
                continue  # parent message, already scanned in the main pass
            reply_ts = float(reply.get("ts", 0))
            if not (oldest <= reply_ts <= latest):
                continue
            _extract_track_ids(reply, track_ids, seen)

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break


def fetch_slack_track_ids(client, channel_id, oldest, latest):
    """Return a de-duplicated, order-preserved list of Spotify track IDs
    found in the channel's messages (including thread replies) between
    oldest and latest (inclusive).

    Note: thread membership is determined by the PARENT message's
    timestamp. A reply posted inside the date range whose parent message
    was posted before `oldest` will still be missed, since conversations
    history won't return that parent at all. This covers the common case
    of threads that started within the scan window."""
    track_ids = []
    seen = set()
    cursor = None
    thread_parents = []  # (thread_ts, reply_count) pairs to fetch replies for

    while True:
        try:
            resp = client.conversations_history(
                channel=channel_id,
                oldest=str(oldest),
                latest=str(latest),
                inclusive=True,
                limit=200,
                cursor=cursor,
            )
        except SlackApiError as e:
            sys.exit(f"Slack API error: {e.response['error']}")

        for msg in resp.get("messages", []):
            _extract_track_ids(msg, track_ids, seen)
            if msg.get("reply_count", 0) > 0 and msg.get("thread_ts"):
                thread_parents.append(msg["thread_ts"])

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    if thread_parents:
        print(f"Checking {len(thread_parents)} thread(s) for replies...")
        for thread_ts in thread_parents:
            fetch_thread_replies(client, channel_id, thread_ts, oldest, latest, track_ids, seen)

    return track_ids


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def resolve_isrcs(sp, track_ids: list[str]) -> tuple[dict[str, str], set[str]]:
    """Given raw Spotify track IDs, return ({track_id: isrc}, unavailable_ids).

    Falls back to using the track_id itself as the ISRC key if no ISRC is
    available (rare, e.g. local files). track_ids that no longer resolve
    (e.g. removed from Spotify) OR that resolve but aren't playable in the
    authenticated user's market (e.g. licensing restrictions — the catalog
    page can still exist and look "normal" even though it's greyed out in
    the app) are returned in unavailable_ids so callers can exclude them.

    NOTE: as of the February 2026 API changes, the batch GET /tracks
    endpoint was removed for Development Mode apps — tracks must be
    fetched one at a time via GET /tracks/{id} (spotipy's sp.track()).

    We pass market="from_token" so Spotify applies track relinking and
    includes an is_playable flag (and a restrictions.reason when false) —
    without a market param, is_playable isn't returned at all and a
    market-restricted track looks identical to a normal one."""
    isrc_by_id = {}
    unavailable = set()
    for tid in track_ids:
        try:
            t = sp.track(tid, market="from_token")
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 404:
                print(f"  Skipping {tid} — no longer available on Spotify")
            else:
                print(f"  Warning: could not fetch track {tid}: {e}")
            unavailable.add(tid)
            continue
        if not t:
            print(f"  Skipping {tid} — no longer available on Spotify")
            unavailable.add(tid)
            continue
        if t.get("is_playable") is False:
            reason = t.get("restrictions", {}).get("reason", "unknown")
            print(f"  Skipping {tid} — not playable in your market (reason: {reason})")
            unavailable.add(tid)
            continue
        isrc = t.get("external_ids", {}).get("isrc")
        # Key by the ORIGINAL requested tid, not t["id"] — Spotify can
        # relink the track to a different regional id, and main() looks
        # this dict up by the tid it found in Slack, not by whatever id
        # comes back from the API.
        isrc_by_id[tid] = isrc or tid
    return isrc_by_id, unavailable


def fetch_playlist_isrcs(sp, playlist_id) -> set[str]:
    """Return the set of ISRCs (or track IDs, for tracks without one)
    already in the playlist.

    NOTE: as of the February 2026 API changes, GET /playlists/{id}/tracks
    was renamed to GET /playlists/{id}/items. Older spotipy versions may
    still call the old path, so this calls the endpoint directly to be
    safe regardless of the installed spotipy version."""
    existing = set()
    # Feb 2026 API migration renamed each playlist entry's "track" key to
    # "item" (tracks.tracks.track -> items.items.item in Spotify's docs).
    fields = "items.item.id,items.item.external_ids.isrc,next"
    limit = 100
    offset = 0

    while True:
        results = sp._get(
            f"playlists/{playlist_id}/items",
            limit=limit,
            offset=offset,
            fields=fields,
            additional_types="track",
        )
        items = results.get("items", [])
        for entry in items:
            track = entry.get("item")
            if not track or not track.get("id"):
                continue
            isrc = track.get("external_ids", {}).get("isrc")
            existing.add(isrc or track["id"])

        if not results.get("next") or not items:
            break
        offset += limit

    return existing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    config = load_config(args.config)

    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    if not slack_token:
        sys.exit("Missing SLACK_BOT_TOKEN in .env")
    slack_client = WebClient(token=slack_token)

    oldest = parse_date_to_ts(config["start_date"])
    latest = parse_date_to_ts(config["end_date"], end_of_day=True)

    print(f"Scanning #{config['slack_channel_id']} from {config['start_date']} to {config['end_date']}...")
    found_ids = fetch_slack_track_ids(slack_client, config["slack_channel_id"], oldest, latest)
    print(f"Found {len(found_ids)} unique Spotify track link(s) in Slack messages.")

    if not found_ids:
        print("Nothing to do.")
        return

    sp_oauth = SpotifyOAuth(
        client_id=os.environ.get("SPOTIFY_CLIENT_ID"),
        client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/callback"),
        scope="playlist-modify-public playlist-modify-private",
        cache_path=os.environ.get("SPOTIFY_CACHE_PATH", ".spotify_cache"),
    )
    sp = spotipy.Spotify(auth_manager=sp_oauth)

    playlist_id = config["spotify_playlist_id"]

    # Resolve found Slack track IDs -> ISRC (or fall back to ID)
    isrc_by_found_id, unavailable_ids = resolve_isrcs(sp, found_ids)
    if unavailable_ids:
        print(f"{len(unavailable_ids)} track(s) no longer available on Spotify — skipping.")

    existing_isrcs = fetch_playlist_isrcs(sp, playlist_id)
    print(f"Playlist currently has {len(existing_isrcs)} track(s).")

    new_ids = []
    seen_this_run = set()
    for tid in found_ids:
        if tid in unavailable_ids:
            continue
        key = isrc_by_found_id.get(tid, tid)
        if key in existing_isrcs or key in seen_this_run:
            continue
        seen_this_run.add(key)
        new_ids.append(tid)

    skipped = len(found_ids) - len(new_ids) - len(unavailable_ids)
    print(f"{len(new_ids)} new track(s) to add ({skipped} already in playlist or duplicate).")

    if not new_ids:
        print("Nothing new to add.")
        return

    if args.dry_run:
        print("\n--dry-run set, not modifying the playlist. Would add:")
        for tid in new_ids:
            print(f"  https://open.spotify.com/track/{tid}")
        return

    for batch in chunked(new_ids, 100):  # Spotify caps adds at 100 per call
        sp.playlist_add_items(playlist_id, [f"spotify:track:{tid}" for tid in batch])

    print(f"Added {len(new_ids)} track(s) to the playlist.")


if __name__ == "__main__":
    main()