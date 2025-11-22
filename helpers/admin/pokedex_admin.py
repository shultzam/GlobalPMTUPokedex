#!/usr/bin/env python3

# /opt/pmtu-pokedex/ddns/

import argparse
import os
import sys
import sqlite3
import shutil
from datetime import datetime

import requests
from requests import Session

"""
PMTU Pokédex Admin Helper
=========================

Examples of Commands
--------------------

1. Check API health
   python3 pokedex_admin.py health

2. Register or update a player name
   python3 pokedex_admin.py register --steam-id 76561198000000000 --steam-name "Allen"

3. Record a capture via the API
   python3 pokedex_admin.py capture --steam-id 76561198000000000 --pokemon "Alolan Raichu"
   python3 pokedex_admin.py capture --steam-id 76561198000000000 --pokemon "Pikachu" --shiny

4. View a player's Pokédex
   python3 pokedex_admin.py dex --steam-id 76561198000000000

5. View the global leaderboard
   python3 pokedex_admin.py leaderboard --limit 20

6. Show caught information for a given Pokémon
   python3 pokedex_admin.py caught-count --pokemon "Pikachu"
   python3 pokedex_admin.py caught-count --pokemon "Pikachu" --shiny
   python3 pokedex_admin.py caught-count --pokemon "Pikachu" --non-shiny

7. Create a backup of the SQLite database
   python3 pokedex_admin.py db-backup

8. List all players (server-side), including totals and shiny counts
   python3 pokedex_admin.py db-list-players

9. Rename a player in the database
   python3 pokedex_admin.py db-rename-player --steam-id 76561198000000000 --name "NewName"

10. Delete a player and all capture data
    python3 pokedex_admin.py db-delete-player --steam-id 76561198000000000

Notes
-----
- API base URL can be overridden with:  --api-base https://yourdomain.com/api
- Database path can be overridden with: --db-path /path/to/pokedex.db
- All default values are drawn from environment variables when present.
"""

# Default API base URL.
DEFAULT_API_BASE = os.environ.get("POKEDEX_API_BASE", "http://127.0.0.1:8080")

# Default SQLite database path.
DEFAULT_DB_PATH = os.environ.get(
    "POKEDEX_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "pokedex.db"),
)

# User-Agent to flag admin usage; matches API-side default.
ADMIN_USER_AGENT = os.environ.get("POKEDEX_ADMIN_UA", "PMTU-Pokedex-Admin")

# Shared HTTP session to carry admin User-Agent on every request.
HTTP: Session = requests.Session()
HTTP.headers.update({"User-Agent": ADMIN_USER_AGENT})

# Performs an HTTP GET to the API health endpoint.
def api_health(api_base):
    url = f"{api_base}/health"
    r = HTTP.get(url, timeout=5)
    print(f"GET {url} -> {r.status_code}")
    print(r.text)

# Registers or updates a player's name with the API.
def api_register(api_base, steam_id, steam_name):
    url = f"{api_base}/v1/register"
    payload = {
        "steam_id": steam_id,
        "steam_name": steam_name,
    }
    r = HTTP.post(url, json=payload, timeout=5)
    print(f"POST {url} -> {r.status_code}")
    if r.ok:
        print(r.json())
    else:
        print(r.text)

# Records a Pokémon capture via the API.
def api_capture(api_base, steam_id, pokemon_name, shiny):
    url = f"{api_base}/v1/capture"
    payload = {
        "steam_id": steam_id,
        "pokemon_name": pokemon_name,
        "shiny": bool(shiny),
    }
    r = HTTP.post(url, json=payload, timeout=5)
    print(f"POST {url} -> {r.status_code}")
    if r.ok:
        print("Capture accepted by server")
    else:
        print(r.text)

def api_uncapture(api_base, steam_id, pokemon_name):
    url = f"{api_base}/v1/uncapture"
    payload = {
        "steam_id": steam_id,
        "pokemon_name": pokemon_name,
    }
    r = HTTP.post(url, json=payload, timeout=5)
    print(f"POST {url} -> {r.status_code}")
    if r.ok:
        data = r.json()
        print(f"Removed {data.get('deleted', 0)} capture(s)")
    else:
        print(r.text)

# Retrieves a player's full Pokédex via the API and prints it.
def api_dex(api_base, steam_id):
    url = f"{api_base}/v1/dex/{steam_id}"
    r = HTTP.get(url, timeout=5)
    print(f"GET {url} -> {r.status_code}")
    if not r.ok:
        print(r.text)
        return
    data = r.json()
    name = data.get("steam_name_safe") or data.get("steam_name") or "Unknown"
    print(f"Dex for {name} ({data.get('steam_id')}):")
    print(f"  total: {data.get('count')}, shiny: {data.get('shiny_count')}")
    for cap in data.get("captures", []):
        flag = " [shiny]" if cap.get("shiny") else ""
        print(f"  {cap.get('pokemon_name')}{flag} at {cap.get('captured_at')}")

# Retrieves the global leaderboard from the API.
def api_leaderboard(api_base, limit):
    url = f"{api_base}/v1/leaderboard?limit={limit}"
    r = HTTP.get(url, timeout=5)
    print(f"GET {url} -> {r.status_code}")
    if not r.ok:
        print(r.text)
        return
    data = r.json()
    for i, e in enumerate(data.get("entries", []), start=1):
        name = e.get("steam_name_safe") or e.get("steam_name") or "Unknown"
        total = e.get("total") or 0
        shinies = e.get("shinies") or 0
        print(f"{i:2d}) {name} ({e.get('steam_id')}) total={total} shiny={shinies}")

# Shows how many players caught a given Pokémon and how many of those are shiny.
def api_caught_count(api_base, pokemon_name):
    url = f"{api_base}/v1/species/{requests.utils.quote(pokemon_name)}/caught"
    r = HTTP.get(url, timeout=5)
    print(f"GET {url} -> {r.status_code}")
    if not r.ok:
        print(r.text)
        return

    data = r.json()
    total = data.get("total_players") or 0
    shiny_count = data.get("shiny_players") or 0

    print(
        f"Who caught {data.get('pokemon_name')} : "
        f"{total} players (shiny={shiny_count})"
    )


# Opens a SQLite connection to the Dex database.
def db_connect(db_path):
    if not os.path.exists(db_path):
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

# Creates a timestamped backup of the SQLite database.
def db_backup(db_path):
    if not os.path.exists(db_path):
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{db_path}.bak.{ts}"
    shutil.copy2(db_path, backup_path)
    print(f"Backup created at {backup_path}")

# Lists all players and their capture totals from the database.
def db_list_players(db_path):
    conn = db_connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.steam_id,
               COALESCE(p.steam_name_safe, p.steam_name, 'Unknown') AS name,
               p.created_at,
               p.last_seen_at,
               COUNT(c.id) AS total,
               SUM(CASE WHEN c.shiny=1 THEN 1 ELSE 0 END) AS shinies
        FROM players p
        LEFT JOIN captures c ON p.steam_id = c.steam_id
        GROUP BY p.steam_id
        ORDER BY total DESC, shinies DESC, name COLLATE NOCASE
        """
    )
    rows = cur.fetchall()
    conn.close()
    for r in rows:
        print(
            f"{r['name']} ({r['steam_id']}): total={r['total']} shiny={r['shinies']} "
            f"created={r['created_at']} last_seen={r['last_seen_at']}"
        )

# Renames a player directly in the SQLite database.
def db_rename_player(db_path, steam_id, new_name):
    conn = db_connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT steam_id FROM players WHERE steam_id=?", (steam_id,))
    row = cur.fetchone()
    if not row:
        print(f"No player found with steam_id={steam_id}")
        conn.close()
        return
    cur.execute(
        """
        UPDATE players
        SET steam_name = ?,
            steam_name_raw = ?,
            steam_name_safe = ?
        WHERE steam_id = ?
        """,
        (new_name, new_name, new_name, steam_id),
    )
    conn.commit()
    conn.close()
    print(f"Updated name for {steam_id} to {new_name}")

# Deletes a player and all their captures from the database.
def db_delete_player(db_path, steam_id):
    conn = db_connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT steam_id FROM players WHERE steam_id=?", (steam_id,))
    if not cur.fetchone():
        print(f"No player found with steam_id={steam_id}")
        conn.close()
        return
    cur.execute("DELETE FROM captures WHERE steam_id=?", (steam_id,))
    cur.execute("DELETE FROM players WHERE steam_id=?", (steam_id,))
    conn.commit()
    conn.close()
    print(f"Deleted player {steam_id} and all captures")

# Builds the command-line argument parser for the script.
def build_parser():
    parser = argparse.ArgumentParser(
        description="PMTU Pokédex admin helper (API and DB tools)"
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"API base URL (default: {DEFAULT_API_BASE})",
    )
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help=f"SQLite DB path (default: {DEFAULT_DB_PATH})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="Check API health")

    p_reg = sub.add_parser("register", help="Register or update a player")
    p_reg.add_argument("--steam-id", required=True)
    p_reg.add_argument("--steam-name", required=True)

    p_cap = sub.add_parser("capture", help="Record a capture via API")
    p_cap.add_argument("--steam-id", required=True)
    p_cap.add_argument("--pokemon", required=True)
    p_cap.add_argument("--shiny", action="store_true")

    p_uncap = sub.add_parser("uncapture", help="Remove a capture via API")
    p_uncap.add_argument("--steam-id", required=True)
    p_uncap.add_argument("--pokemon", required=True)

    p_dex = sub.add_parser("dex", help="Show a player's dex via API")
    p_dex.add_argument("--steam-id", required=True)

    p_lb = sub.add_parser("leaderboard", help="Show leaderboard via API")
    p_lb.add_argument("--limit", type=int, default=25)

    p_wc = sub.add_parser("caught-count", help="Show caught information for a Pokémon via API")
    p_wc.add_argument("--pokemon", required=True)

    sub.add_parser("db-backup", help="Backup the SQLite database")
    sub.add_parser("db-list-players", help="List players and capture counts")

    p_rn = sub.add_parser("db-rename-player", help="Rename a player in the DB")
    p_rn.add_argument("--steam-id", required=True)
    p_rn.add_argument("--name", required=True)

    p_del = sub.add_parser("db-delete-player", help="Delete a player and captures")
    p_del.add_argument("--steam-id", required=True)

    p_lbc = sub.add_parser("leaderboard-completion", help="Show completion leaderboard via API")
    p_lbc.add_argument("--limit", type=int, default=15)

    return parser

# Main entry point. Dispatches commands to the appropriate function.
def main():
    parser = build_parser()
    args = parser.parse_args()

    api_base = args.api_base.rstrip("/")
    db_path = args.db_path

    if args.command == "health":
        api_health(api_base)
    elif args.command == "register":
        api_register(api_base, args.steam_id, args.steam_name)
    elif args.command == "capture":
        api_capture(api_base, args.steam_id, args.pokemon, args.shiny)
    elif args.command == "uncapture":
        api_uncapture(api_base, args.steam_id, args.pokemon)
    elif args.command == "dex":
        api_dex(api_base, args.steam_id)
    elif args.command == "leaderboard":
        api_leaderboard(api_base, args.limit)
    elif args.command == "caught-count":
        api_caught_count(api_base, args.pokemon)
    elif args.command == "db-backup":
        db_backup(db_path)
    elif args.command == "db-list-players":
        db_list_players(db_path)
    elif args.command == "db-rename-player":
        db_rename_player(db_path, args.steam_id, args.name)
    elif args.command == "db-delete-player":
        db_delete_player(db_path, args.steam_id)
    elif args.command == "leaderboard-completion":
        api_leaderboard_completion(api_base, args.limit)
    else:
        parser.print_help()
        return 1

    return 0

# Retrieves the "most complete" Pokédex leaderboard via the API.
def api_leaderboard_completion(api_base, limit):
    url = f"{api_base}/v1/leaderboard/completion?limit={limit}"
    r = HTTP.get(url, timeout=5)
    print(f"GET {url} -> {r.status_code}")
    if not r.ok:
        print(r.text)
        return

    data = r.json()
    max_species = data.get("max_species") or 0
    entries = data.get("entries", [])

    if not entries:
        print("No entries yet for completion leaderboard.")
        return

    print(f"Most complete Pokédex (top {len(entries)}), max species = {max_species}")
    for i, e in enumerate(entries, start=1):
        name = e.get("steam_name_safe") or e.get("steam_name") or "Unknown"
        unique = e.get("unique_species") or 0
        max_s = e.get("max_species") or max_species
        pct = 0
        if max_s and max_s > 0:
            pct = int(round(unique * 100.0 / max_s))
        print(
            f"{i:2d}) {name} ({e.get('steam_id')}): "
            f"{unique}/{max_s} ({pct}%)"
        )

# Script entry.
if __name__ == "__main__":
    raise SystemExit(main())
