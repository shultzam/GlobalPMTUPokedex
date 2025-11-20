from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timezone
import sqlite3, os, unicodedata, re

# Logging.
import logging
from logging.handlers import RotatingFileHandler
import os
import time

MAX_SPECIES_COUNT = int(os.environ.get("POKEDEX_MAX_SPECIES", "1025"))
DB_PATH = os.environ.get("DB_PATH", "/data/pokedex.db")
MAX_NAME_LEN = 64
SAFE_CHARS_RE = re.compile(r"[^0-9A-Za-z\u00C0-\uFFFF \-_.()!@#\$%&\+\=,:;]")

def make_safe_name(name: Optional[str]) -> str:
    if not name:
        return "Unknown"
    n = unicodedata.normalize("NFC", name)
    n = "".join(ch for ch in n if unicodedata.category(ch) not in ("Cc","Cf"))
    n = re.sub(r"\s+", " ", n).strip()
    n = SAFE_CHARS_RE.sub("", n)
    if len(n) > MAX_NAME_LEN:
        n = n[:MAX_NAME_LEN].rstrip()
    return n or "Unknown"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    pokedex_logger("Initialized DB")

    # Players table.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS players(
      steam_id TEXT PRIMARY KEY,
      steam_name TEXT,
      steam_name_raw TEXT,
      steam_name_safe TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      last_seen_at TEXT
    );
    """)

    # Captures table.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS captures(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      steam_id TEXT NOT NULL,
      pokemon_name TEXT NOT NULL,
      shiny INTEGER DEFAULT 0,
      captured_at TEXT NOT NULL,
      FOREIGN KEY(steam_id) REFERENCES players(steam_id)
    );
    """)

    # Helpful non-unique indexes.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_captures_pokemon ON captures(pokemon_name);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_captures_steam ON captures(steam_id);")

    # Enforce one row per (steam_id, pokemon_name).
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_captures_unique_player_species
    ON captures(steam_id, pokemon_name);
    """)

    conn.commit()
    conn.close()

# Init logging.
LOG_DIR = "/var/log/pmtu-pokedex"
LOG_PATH = os.path.join(LOG_DIR, "pokedex.log")

os.makedirs(LOG_DIR, exist_ok=True)

pokedex_logger = logging.getLogger("pmtu_pokedex")
pokedex_logger.setLevel(logging.INFO)

# Rotate at 5 MB per file, keep 5 backups
file_handler = RotatingFileHandler(
    LOG_PATH,
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
))

# Also log to stdout so docker logs still show app events
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
))

# Avoid adding handlers twice if app reloads
if not pokedex_logger.handlers:
    pokedex_logger.addHandler(file_handler)
    pokedex_logger.addHandler(stream_handler)

# Create the app.
app = FastAPI(title="TTS Global Pokedex", root_path="/api")

@app.on_event("startup")
def startup():
    os.makedirs("/data", exist_ok=True)
    init_db()

class RegisterReq(BaseModel):
    steam_id: str = Field(..., min_length=3, max_length=64)
    steam_name: Optional[str] = None

class CaptureReq(BaseModel):
    steam_id: str
    pokemon_name: str = Field(..., min_length=1, max_length=64)
    shiny: Optional[bool] = False
    captured_at: Optional[str] = None

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/v1/register")
def register(req: RegisterReq):
    # Log it.
    pokedex_logger.info(f"{req.steam_name} ({req.steam_id}) registered")

    now = datetime.now(timezone.utc).isoformat()
    safe = make_safe_name(req.steam_name)
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT steam_id FROM players WHERE steam_id=?", (req.steam_id,))
    if cur.fetchone():
        cur.execute("""
            UPDATE players
            SET steam_name=?, steam_name_raw=?, steam_name_safe=?,
                updated_at=?, last_seen_at=?
            WHERE steam_id=?""",
            (req.steam_name, req.steam_name, safe, now, now, req.steam_id))
    else:
        cur.execute("""
            INSERT INTO players(steam_id, steam_name, steam_name_raw, steam_name_safe,
                                created_at, updated_at, last_seen_at)
            VALUES(?,?,?,?,?,?,?)""",
            (req.steam_id, req.steam_name, req.steam_name, safe, now, now, now))
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "steam_id": req.steam_id,
        "steam_name": req.steam_name,
        "steam_name_safe": safe
    }

@app.post("/v1/capture")
def capture(req: CaptureReq):
    # Log it.
    pokedex_logger.info(
        f"{req.steam_id} captured {req.pokemon_name} {'[shiny]' if req.shiny else ''}"
    )

    # Ignore Mega and Gmax forms completely.
    name_lower = req.pokemon_name.lower()
    if name_lower.startswith("mega ") or name_lower.startswith("gmax "):
        return {"ok": True, "ignored": True, "reason": "mega-gmax-not-tracked"}

    # Get the current time.
    if req.captured_at is None:
        req.captured_at = datetime.now(timezone.utc).isoformat()

    # Get access to the DB and ensure the user exists.
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM players WHERE steam_id=?", (req.steam_id,))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Player not registered")

    # Insert if not present; ignore if already exists
    cur.execute(
        """
        INSERT OR IGNORE INTO captures(steam_id, pokemon_name, shiny, captured_at)
        VALUES(?,?,?,?)
        """,
        (req.steam_id, req.pokemon_name, 1 if req.shiny else 0, req.captured_at),
    )
    # If this is a shiny capture, upgrade any existing non-shiny row to shiny
    if req.shiny:
        cur.execute(
            """
            UPDATE captures
            SET shiny = 1
            WHERE steam_id = ? AND pokemon_name = ? AND shiny = 0
            """,
            (req.steam_id, req.pokemon_name),
        )
    conn.commit(); conn.close()

    return {"ok": True}

@app.get("/v1/dex/{steam_id}")
def dex(steam_id: str):
    # Log it.
    pokedex_logger.info(f"Getting dex for {steam_id}")

    conn = db(); cur = conn.cursor()
    cur.execute("SELECT steam_name, steam_name_safe FROM players WHERE steam_id=?", (steam_id,))
    row = cur.fetchone()
    steam_name = row["steam_name"] if row else None
    steam_name_safe = row["steam_name_safe"] if row else None
    cur.execute("""
      SELECT pokemon_name, shiny, captured_at
      FROM captures WHERE steam_id=?
      ORDER BY pokemon_name COLLATE NOCASE
    """, (steam_id,))
    caps = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "steam_id": steam_id,
        "steam_name": steam_name,
        "steam_name_safe": steam_name_safe or steam_name,
        "count": len(caps),
        "shiny_count": sum(1 for c in caps if c["shiny"]),
        "captures": caps
    }

@app.get("/v1/leaderboard")
def leaderboard(limit: int = 25):
    # Log it.
    pokedex_logger.info(f"Getting leaderboard")

    conn = db(); cur = conn.cursor()
    cur.execute("""
      SELECT p.steam_id, p.steam_name, p.steam_name_safe,
             COUNT(c.id) AS total,
             SUM(CASE WHEN c.shiny=1 THEN 1 ELSE 0 END) AS shinies
      FROM players p
      LEFT JOIN captures c ON p.steam_id = c.steam_id
      GROUP BY p.steam_id
      ORDER BY total DESC, shinies DESC, p.steam_name COLLATE NOCASE
      LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {"entries": rows}

@app.get("/v1/species/{pokemon_name}/caught")
def who_caught(pokemon_name: str):
    # Log it.
    pokedex_logger.info(f"Getting caught info for {pokemon_name}")

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
          COUNT(*) AS total_players,
          SUM(CASE WHEN shiny = 1 THEN 1 ELSE 0 END) AS shiny_players
        FROM captures
        WHERE pokemon_name = ?
        """,
        (pokemon_name,),
    )

    row = cur.fetchone()
    conn.close()

    total_players = row["total_players"] or 0
    shiny_players = row["shiny_players"] or 0

    return {
        "pokemon_name": pokemon_name,
        "total_players": total_players,
        "shiny_players": shiny_players
    }

@app.get("/v1/leaderboard/completion")
def leaderboard_completion(limit: int = 15):
    conn = db(); cur = conn.cursor()
    cur.execute(
        """
        SELECT p.steam_id,
               p.steam_name,
               p.steam_name_safe,
               COUNT(DISTINCT c.pokemon_name) AS unique_species
        FROM players p
        LEFT JOIN captures c
          ON p.steam_id = c.steam_id
         AND c.pokemon_name NOT LIKE 'Mega %%'
         AND c.pokemon_name NOT LIKE 'Gmax %%'
        GROUP BY p.steam_id
        HAVING unique_species > 0
        ORDER BY unique_species DESC, p.steam_name COLLATE NOCASE
        LIMIT ?
        """,
        (limit,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    for r in rows:
        r["max_species"] = MAX_SPECIES_COUNT
        r["completion_ratio"] = (r["unique_species"] or 0) / MAX_SPECIES_COUNT if MAX_SPECIES_COUNT > 0 else 0.0

    return {
        "max_species": MAX_SPECIES_COUNT,
        "entries": rows,
    }

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    client_ip = request.client.host if request.client else "unknown"
    method = request.method
    path = request.url.path
    ua = request.headers.get("user-agent", "-")

    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        status = 500
        pokedex_logger.exception("Unhandled exception during request")
        raise
    finally:
        duration_ms = (time.time() - start) * 1000.0
        pokedex_logger.info(
            "%s %s %s %d %.2fms UA=%s",
            client_ip,
            method,
            path,
            status,
            duration_ms,
            ua,
        )

    return response