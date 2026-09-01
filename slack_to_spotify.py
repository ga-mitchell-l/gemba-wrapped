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

For a Spotify-Wrapped-style summary of posting activity in the channel
(top posters, most-reacted song, genre breakdown, etc.) see wrapped.py.

Setup:
    pip install slack_sdk spotipy python-dotenv

    1. Copy .env.example to .env and fill in your credentials.
    2. Copy config.example.json to config.json and fill in your settings.
    3. Run:  python slack_to_spotify.py
       (add --dry-run to preview without modifying the playlist)
"""

import argparse

from dotenv import load_dotenv

from gemba_common import (
    load_config,
    parse_date_to_ts,
    get_slack_client,
    get_spotify_client,
    get_channel_name,
    get_playlist_name,
    fetch_slack_track_mentions,
    resolve_track_info,
    fetch_playlist_isrcs,
    chunked,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--dry-run", action="store_true", help="Preview without modifying the playlist")
    args = parser.parse_args()

    load_dotenv()
    config = load_config(args.config)

    slack_client = get_slack_client()

    oldest = parse_date_to_ts(config["start_date"])
    latest = parse_date_to_ts(config["end_date"], end_of_day=True)

    channel_id = config["slack_channel_id"]
    channel_name = get_channel_name(slack_client, channel_id)

    print(f"Scanning {channel_name} from {config['start_date']} to {config['end_date']}...")
    found_ids, _mentions = fetch_slack_track_mentions(slack_client, channel_id, oldest, latest)
    print(f"Found {len(found_ids)} unique Spotify track link(s) in Slack messages.")

    if not found_ids:
        print("Nothing to do.")
        return

    sp = get_spotify_client()
    playlist_id = config["spotify_playlist_id"]
    playlist_name = get_playlist_name(sp, playlist_id)

    # Resolve found Slack track IDs -> {isrc, name, artist_id, artist_name}
    info_by_found_id, unavailable_ids = resolve_track_info(sp, found_ids)
    if unavailable_ids:
        print(f"{len(unavailable_ids)} track(s) no longer available on Spotify — skipping.")

    existing_isrcs = fetch_playlist_isrcs(sp, playlist_id)
    print(f'"{playlist_name}" currently has {len(existing_isrcs)} track(s).')

    new_ids = []
    seen_this_run = set()
    for tid in found_ids:
        if tid in unavailable_ids:
            continue
        key = info_by_found_id.get(tid, {}).get("isrc", tid)
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
        print(f'\n--dry-run set, not modifying "{playlist_name}". Would add:')
        for tid in new_ids:
            print(f"  https://open.spotify.com/track/{tid}")
        return

    for batch in chunked(new_ids, 100):  # Spotify caps adds at 100 per call
        sp.playlist_add_items(playlist_id, [f"spotify:track:{tid}" for tid in batch])

    print(f'Added {len(new_ids)} track(s) from {channel_name} to "{playlist_name}".')


if __name__ == "__main__":
    main()