#!/usr/bin/env python3
"""
wrapped.py

A Spotify-Wrapped-style summary of a Slack channel's music-sharing
activity over the date range in config.json: top posters, the
most-reacted-to song, most-posted songs/artists, and a genre breakdown.

Read-only — this never modifies the Spotify playlist. For syncing
tracks into the playlist, see slack_to_spotify.py.

Setup:
    Uses the same .env and config.json as slack_to_spotify.py.
    pip install slack_sdk spotipy python-dotenv

Usage:
    python wrapped.py
    python wrapped.py --config config.json   # optional, this is the default
"""

import argparse
from collections import Counter, defaultdict

from dotenv import load_dotenv

from gemba_common import (
    load_config,
    parse_date_to_ts,
    get_slack_client,
    get_spotify_client,
    get_channel_name,
    fetch_slack_track_mentions,
    resolve_track_info,
    get_user_display_name,
    get_artist_genres,
)


def print_wrapped_summary(sp, slack_client, mentions, info_by_id, channel_name):
    if not mentions:
        print("No tracks posted in this date range — nothing to wrap up.")
        return

    user_name_cache = {}
    artist_genre_cache = {}

    print(f"\n{'=' * 40}")
    print(f"  {channel_name} Wrapped")
    print(f"{'=' * 40}")

    # Top posters (by number of track links posted, not distinct songs —
    # posting the same bop three times still counts three times).
    poster_counts = Counter(m["user_id"] for m in mentions if m["user_id"])
    if poster_counts:
        print("\nTop posters:")
        for i, (user_id, count) in enumerate(poster_counts.most_common(5), 1):
            name = get_user_display_name(slack_client, user_id, user_name_cache)
            print(f"  {i}. {name} — {count} song(s) posted")

    # Song with the most Slack reactions (summed across all times it was
    # posted, in case the same track was shared more than once).
    reactions_by_track = defaultdict(int)
    for m in mentions:
        reactions_by_track[m["track_id"]] += m["reactions"]
    if reactions_by_track:
        top_tid, top_reactions = max(reactions_by_track.items(), key=lambda kv: kv[1])
        if top_reactions > 0:
            info = info_by_id.get(top_tid)
            label = f'{info["name"]} — {info["artist_name"]}' if info else top_tid
            print(f"\nMost-reacted song: {label}")
            print(f"  {top_reactions} reaction(s) · https://open.spotify.com/track/{top_tid}")
        else:
            print("\nMost-reacted song: no reactions on any track this period.")

    # Most-posted songs (repost count, not uniqueness).
    track_mention_counts = Counter(m["track_id"] for m in mentions)
    repeats = [(tid, c) for tid, c in track_mention_counts.most_common(5) if c > 1]
    if repeats:
        print("\nMost-posted songs:")
        for i, (tid, count) in enumerate(repeats, 1):
            info = info_by_id.get(tid)
            label = f'{info["name"]} — {info["artist_name"]}' if info else tid
            print(f"  {i}. {label} — posted {count} times")

    # Top artists by number of posts referencing them.
    artist_mention_counts = Counter()
    for m in mentions:
        info = info_by_id.get(m["track_id"])
        if info:
            artist_mention_counts[info["artist_name"]] += 1
    if artist_mention_counts:
        print("\nTop artists:")
        for i, (artist_name, count) in enumerate(artist_mention_counts.most_common(5), 1):
            print(f"  {i}. {artist_name} — {count} post(s)")

    # Genre breakdown, via each posted track's primary artist.
    genre_counts = Counter()
    for m in mentions:
        info = info_by_id.get(m["track_id"])
        if not info or not info.get("artist_id"):
            continue
        for genre in get_artist_genres(sp, info["artist_id"], artist_genre_cache):
            genre_counts[genre] += 1
    if genre_counts:
        total = sum(genre_counts.values())
        print("\nGenre breakdown:")
        for genre, count in genre_counts.most_common(8):
            pct = 100 * count / total
            print(f"  {genre}: {pct:.0f}%")
    else:
        print("\nGenre breakdown: no genre data available for these artists.")

    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    load_dotenv()
    config = load_config(args.config)

    slack_client = get_slack_client()

    oldest = parse_date_to_ts(config["start_date"])
    latest = parse_date_to_ts(config["end_date"], end_of_day=True)

    channel_id = config["slack_channel_id"]
    channel_name = get_channel_name(slack_client, channel_id)

    print(f"Scanning {channel_name} from {config['start_date']} to {config['end_date']}...")
    found_ids, mentions = fetch_slack_track_mentions(slack_client, channel_id, oldest, latest)
    print(f"Found {len(found_ids)} unique Spotify track link(s) ({len(mentions)} total posting(s)).")

    if not found_ids:
        print("Nothing to do.")
        return

    sp = get_spotify_client()

    info_by_found_id, unavailable_ids = resolve_track_info(sp, found_ids)
    if unavailable_ids:
        print(f"{len(unavailable_ids)} track(s) no longer available on Spotify — excluded from stats.")

    print_wrapped_summary(sp, slack_client, mentions, info_by_found_id, channel_name)


if __name__ == "__main__":
    main()