"""
gemba_common.py

Shared helpers for slack_to_spotify.py and wrapped.py: config loading,
Slack track-link scanning (including thread replies), and Spotify track
resolution. Not meant to be run directly.
"""

import os
import re
import sys
import json
from datetime import datetime, timezone

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


def get_slack_client() -> WebClient:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        sys.exit("Missing SLACK_BOT_TOKEN in .env")
    return WebClient(token=token)


def get_spotify_client() -> spotipy.Spotify:
    sp_oauth = SpotifyOAuth(
        client_id=os.environ.get("SPOTIFY_CLIENT_ID"),
        client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/callback"),
        scope="playlist-modify-public playlist-modify-private",
        cache_path=os.environ.get("SPOTIFY_CACHE_PATH", ".spotify_cache"),
    )
    return spotipy.Spotify(auth_manager=sp_oauth)


def get_channel_name(slack_client, channel_id: str) -> str:
    """Returns '#channel-name', falling back to the raw ID if the lookup fails."""
    try:
        info = slack_client.conversations_info(channel=channel_id)
        return "#" + info["channel"].get("name", channel_id)
    except SlackApiError:
        return channel_id


def get_playlist_name(sp, playlist_id: str) -> str:
    """Returns the playlist's name, falling back to the raw ID if the lookup fails."""
    try:
        return sp.playlist(playlist_id, fields="name")["name"]
    except Exception:
        return playlist_id


def _find_track_ids_in_message(msg) -> list[str]:
    """Return every Spotify track ID mentioned in a message's text or
    attachments, in order, WITHOUT deduping (a message could reasonably
    link the same track twice, but that's rare enough not to matter)."""
    text = msg.get("text", "")
    attachment_texts = " ".join(
        a.get("title", "") + " " + a.get("from_url", "")
        for a in msg.get("attachments", [])
    )
    full_text = text + " " + attachment_texts

    tids = []
    for pattern in (TRACK_URL_RE, TRACK_URI_RE):
        tids.extend(match.group(1) for match in pattern.finditer(full_text))
    return tids


def _message_reaction_count(msg) -> int:
    return sum(r.get("count", 0) for r in msg.get("reactions", []))


def _fetch_thread_replies(client, channel_id, thread_ts, oldest, latest, on_message):
    """Fetch every reply in a thread within [oldest, latest] and call
    on_message(reply) for each. The parent message is skipped since it's
    already handled as part of the main channel history scan."""
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
            on_message(reply)

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break


def fetch_slack_track_mentions(client, channel_id, oldest, latest):
    """Scan the channel's messages (including thread replies) between
    oldest and latest (inclusive) for Spotify track links.

    Returns (found_ids, mentions):
      found_ids: de-duplicated, order-preserved list of track IDs — used
                 to drive the playlist sync.
      mentions:  one entry per track-link occurrence (NOT deduped), each
                 {"track_id", "user_id", "reactions"} — used to drive the
                 wrapped summary (who posted what, how it landed).

    Note: thread membership is determined by the PARENT message's
    timestamp. A reply posted inside the date range whose parent message
    was posted before `oldest` will still be missed, since conversations
    history won't return that parent at all. This covers the common case
    of threads that started within the scan window."""
    found_ids = []
    seen = set()
    mentions = []
    thread_parents = []

    def handle_message(msg):
        tids = _find_track_ids_in_message(msg)
        if not tids:
            return
        reactions = _message_reaction_count(msg)
        user_id = msg.get("user")
        for tid in tids:
            if tid not in seen:
                seen.add(tid)
                found_ids.append(tid)
            mentions.append({"track_id": tid, "user_id": user_id, "reactions": reactions})

    cursor = None
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
            handle_message(msg)
            if msg.get("reply_count", 0) > 0 and msg.get("thread_ts"):
                thread_parents.append(msg["thread_ts"])

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    if thread_parents:
        print(f"Checking {len(thread_parents)} thread(s) for replies...")
        for thread_ts in thread_parents:
            _fetch_thread_replies(client, channel_id, thread_ts, oldest, latest, handle_message)

    return found_ids, mentions


def resolve_track_info(sp, track_ids: list[str]) -> tuple[dict[str, dict], set[str]]:
    """Given raw Spotify track IDs, return ({track_id: info}, unavailable_ids).

    info = {"isrc", "name", "artist_id", "artist_name"} — isrc falls back
    to the track_id itself if no ISRC is available (rare, e.g. local
    files); artist_id/artist_name are the track's primary (first-listed)
    artist, used for wrapped's top-artists and genre breakdown.

    track_ids that no longer resolve (e.g. removed from Spotify) OR that
    resolve but aren't playable in the authenticated user's market (e.g.
    licensing restrictions — the catalog page can still exist and look
    "normal" even though it's greyed out in the app) are returned in
    unavailable_ids so callers can exclude them.

    NOTE: as of the February 2026 API changes, the batch GET /tracks
    endpoint was removed for Development Mode apps — tracks must be
    fetched one at a time via GET /tracks/{id} (spotipy's sp.track()).

    We pass market="from_token" so Spotify applies track relinking and
    includes an is_playable flag (and a restrictions.reason when false) —
    without a market param, is_playable isn't returned at all and a
    market-restricted track looks identical to a normal one."""
    info_by_id = {}
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
        artists = t.get("artists") or []
        primary_artist = artists[0] if artists else {}

        # Key by the ORIGINAL requested tid, not t["id"] — Spotify can
        # relink the track to a different regional id, and callers look
        # this dict up by the tid found in Slack, not by whatever id
        # comes back from the API.
        info_by_id[tid] = {
            "isrc": isrc or tid,
            "name": t.get("name", tid),
            "artist_id": primary_artist.get("id"),
            "artist_name": primary_artist.get("name", "Unknown Artist"),
        }
    return info_by_id, unavailable


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


def get_user_display_name(client, user_id, cache) -> str:
    if not user_id:
        return "Unknown user"
    if user_id in cache:
        return cache[user_id]
    try:
        info = client.users_info(user=user_id)
        profile = info["user"].get("profile", {})
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or info["user"].get("name")
            or user_id
        )
    except SlackApiError:
        name = user_id
    cache[user_id] = name
    return name


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]