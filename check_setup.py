#!/usr/bin/env python3
"""
check_setup.py

Verifies that your .env and config.json are correctly set up for
slack_to_spotify.py and wrapped.py, BEFORE you run either. Both scripts
share the same .env/config.json, so this only needs to be run once.
Checks:

  1. .env file exists and has the required keys
  2. config.json exists and has the required keys
  3. Slack token is valid and can see the configured channel
  4. Spotify credentials are valid and can see the configured playlist

Usage:
    pip install slack_sdk spotipy python-dotenv
    python check_setup.py
    python check_setup.py --config config.json   # optional, this is the default
"""

import os
import sys
import json
import argparse

from dotenv import load_dotenv

CHECK = "\u2713"   # ✓
CROSS = "\u2717"   # ✗
WARN = "\u26a0"    # ⚠


def ok(msg):
    print(f"  {CHECK} {msg}")


def fail(msg):
    print(f"  {CROSS} {msg}")


def warn(msg):
    print(f"  {WARN} {msg}")


def section(title):
    print(f"\n{title}")
    print("-" * len(title))


def check_files(config_path):
    section("Files")
    problems = []

    if os.path.exists(".env"):
        ok(".env found")
    else:
        fail(".env not found in current directory")
        problems.append("missing .env")

    if os.path.exists(config_path):
        ok(f"{config_path} found")
    else:
        fail(f"{config_path} not found in current directory")
        problems.append("missing config.json")

    return problems


def check_env_vars():
    section("Environment variables (.env)")
    load_dotenv()

    required = {
        "SLACK_BOT_TOKEN": "xoxb-",
        "SPOTIFY_CLIENT_ID": None,
        "SPOTIFY_CLIENT_SECRET": None,
    }
    optional = ["SPOTIFY_REDIRECT_URI", "SPOTIFY_CACHE_PATH"]

    problems = []
    values = {}

    for key, expected_prefix in required.items():
        val = os.environ.get(key)
        if not val:
            fail(f"{key} is not set")
            problems.append(key)
            continue
        if expected_prefix and not val.startswith(expected_prefix):
            warn(f"{key} is set but doesn't start with '{expected_prefix}' — double check it's the right token type")
        else:
            ok(f"{key} is set")
        values[key] = val

    for key in optional:
        val = os.environ.get(key)
        if val:
            ok(f"{key} is set ({val})")
        else:
            warn(f"{key} not set — will fall back to script default")

    return values, problems


def check_config(config_path):
    section(f"Config file ({config_path})")
    problems = []
    config = {}

    if not os.path.exists(config_path):
        fail(f"Can't check contents — {config_path} doesn't exist")
        return config, ["missing file"]

    try:
        with open(config_path) as f:
            config = json.load(f)
        ok("Valid JSON")
    except json.JSONDecodeError as e:
        fail(f"Invalid JSON: {e}")
        return config, ["invalid json"]

    required = ["slack_channel_id", "spotify_playlist_id", "start_date", "end_date"]
    for key in required:
        if config.get(key):
            ok(f"{key}: {config[key]}")
        else:
            fail(f"{key} is missing or empty")
            problems.append(key)

    # sanity check date range
    from datetime import datetime
    try:
        start = datetime.strptime(config.get("start_date", ""), "%Y-%m-%d")
        end = datetime.strptime(config.get("end_date", ""), "%Y-%m-%d")
        if start > end:
            fail(f"start_date ({config['start_date']}) is after end_date ({config['end_date']})")
            problems.append("date range reversed")
        else:
            ok("Date range is valid (start_date <= end_date)")
    except ValueError:
        if config.get("start_date") or config.get("end_date"):
            fail("start_date/end_date must be in YYYY-MM-DD format")
            problems.append("bad date format")

    return config, problems


def check_slack(config):
    section("Slack connectivity")
    problems = []

    token = os.environ.get("SLACK_BOT_TOKEN")
    channel_id = config.get("slack_channel_id")

    if not token:
        fail("Skipping — SLACK_BOT_TOKEN not set")
        return ["no token"]

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        fail("slack_sdk not installed — run: pip install slack_sdk")
        return ["slack_sdk missing"]

    client = WebClient(token=token)

    # 1. Is the token itself valid?
    try:
        auth = client.auth_test()
        ok(f"Token is valid (bot: {auth.get('user')}, workspace: {auth.get('team')})")
    except SlackApiError as e:
        fail(f"Token rejected by Slack: {e.response['error']}")
        return ["invalid token"]

    # 2. Does the token have history scope + can it see the channel?
    if not channel_id:
        warn("No slack_channel_id in config — skipping channel check")
        return problems

    try:
        info = client.conversations_info(channel=channel_id)
        chan = info["channel"]
        name = chan.get("name", channel_id)
        is_member = chan.get("is_member", False)
        ok(f"Channel found: #{name}")
        if is_member:
            ok("Bot is a member of the channel")
        else:
            fail(f"Bot is NOT a member of #{name} — run /invite @yourbotname in that channel")
            problems.append("bot not in channel")
    except SlackApiError as e:
        err = e.response["error"]
        if err == "channel_not_found":
            fail(f"Channel ID '{channel_id}' not found — double check it (right-click channel > View channel details > copy Channel ID)")
        elif err == "missing_scope":
            fail("Token is missing the channels:history / groups:history scope")
        else:
            fail(f"Error looking up channel: {err}")
        problems.append(err)

    # 3. Can it actually read history? (separate from conversations_info)
    try:
        client.conversations_history(channel=channel_id, limit=1)
        ok("Can read message history in the channel")
    except SlackApiError as e:
        fail(f"Cannot read history: {e.response['error']}")
        problems.append("history read failed")

    # 4. Can it look up user profiles? Only needed for wrapped.py, which
    # resolves poster IDs to display names — not required for
    # slack_to_spotify.py's sync, so this is a warning, not a failure.
    try:
        client.users_info(user=auth.get("user_id"))
        ok("Can look up user profiles (needed for wrapped.py's poster names)")
    except SlackApiError as e:
        if e.response["error"] == "missing_scope":
            warn("Token is missing the users:read scope — wrapped.py will still run, "
                 "but will show raw Slack user IDs instead of names")
        else:
            warn(f"Could not verify user-lookup permission: {e.response['error']}")

    return problems


def check_spotify(config):
    section("Spotify connectivity")
    problems = []

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    playlist_id = config.get("spotify_playlist_id")

    if not client_id or not client_secret:
        fail("Skipping — SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set")
        return ["no credentials"]

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError:
        fail("spotipy not installed — run: pip install spotipy")
        return ["spotipy missing"]

    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/callback")
    cache_path = os.environ.get("SPOTIFY_CACHE_PATH", ".spotify_cache")

    try:
        auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope="playlist-modify-public playlist-modify-private",
            cache_path=cache_path,
            open_browser=True,
        )
        sp = spotipy.Spotify(auth_manager=auth_manager)
        me = sp.current_user()
        ok(f"Authenticated as Spotify user: {me.get('display_name') or me.get('id')}")
    except Exception as e:
        fail(f"Spotify auth failed: {e}")
        problems.append("auth failed")
        return problems

    if not playlist_id:
        warn("No spotify_playlist_id in config — skipping playlist check")
        return problems

    try:
        playlist = sp.playlist(playlist_id, fields="name,owner.id,collaborative")
        ok(f"Playlist found: \"{playlist['name']}\"")

        # check we can actually modify it
        my_id = me.get("id")
        owner_id = playlist.get("owner", {}).get("id")
        is_collab = playlist.get("collaborative", False)
        if owner_id == my_id or is_collab:
            ok("You have permission to add tracks to this playlist")
        else:
            warn(f"Playlist is owned by '{owner_id}' and not marked collaborative — adding tracks may fail")
    except Exception as e:
        fail(f"Could not access playlist '{playlist_id}': {e}")
        problems.append("playlist access failed")

    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    print("Checking slack_to_spotify.py setup...")

    all_problems = []
    all_problems += check_files(args.config)
    env_values, env_problems = check_env_vars()
    all_problems += env_problems
    config, config_problems = check_config(args.config)
    all_problems += config_problems

    # Only try live connectivity checks if the basics are in place
    if not env_problems:
        all_problems += check_slack(config)
        all_problems += check_spotify(config)
    else:
        section("Slack & Spotify connectivity")
        warn("Skipped — fix the missing environment variables above first")

    section("Summary")
    if not all_problems:
        print(f"  {CHECK} Everything looks good — you're ready to run slack_to_spotify.py")
    else:
        print(f"  {CROSS} {len(all_problems)} issue(s) found — see details above")
        sys.exit(1)


if __name__ == "__main__":
    main()