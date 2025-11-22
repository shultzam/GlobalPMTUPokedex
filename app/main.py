# Imports.
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Tuple
from datetime import datetime, timezone
import sqlite3, os, unicodedata, re
import threading
from queue import Queue, Full
from concurrent.futures import Future, TimeoutError

# Logging.
import logging
from logging.handlers import RotatingFileHandler
import os
import time

# Global defines.
MAX_SPECIES_COUNT = int(os.environ.get("POKEDEX_MAX_SPECIES", "1998"))
DB_PATH = os.environ.get("DB_PATH", "/data/pokedex.db")
DB_TIMEOUT = float(os.environ.get("POKEDEX_DB_TIMEOUT_SEC", "30"))
DB_BUSY_TIMEOUT_MS = int(os.environ.get("POKEDEX_DB_BUSY_TIMEOUT_MS", "5000"))
CAPTURE_QUEUE_MAXSIZE = int(os.environ.get("POKEDEX_CAPTURE_QUEUE_MAXSIZE", "500"))
CAPTURE_WORKERS = int(os.environ.get("POKEDEX_CAPTURE_WORKERS", "1"))
CAPTURE_PROCESS_TIMEOUT = float(os.environ.get("POKEDEX_CAPTURE_PROCESS_TIMEOUT_SEC", "60"))
REGISTER_QUEUE_MAXSIZE = int(os.environ.get("POKEDEX_REGISTER_QUEUE_MAXSIZE", "500"))
REGISTER_WORKERS = int(os.environ.get("POKEDEX_REGISTER_WORKERS", "1"))
REGISTER_PROCESS_TIMEOUT = float(os.environ.get("POKEDEX_REGISTER_PROCESS_TIMEOUT_SEC", "60"))
CAPTURE_IMMEDIATE_ACK = os.environ.get("POKEDEX_CAPTURE_IMMEDIATE_ACK", "false").strip().lower() in ("1", "true", "yes", "on")
REGISTER_IMMEDIATE_ACK = os.environ.get("POKEDEX_REGISTER_IMMEDIATE_ACK", "true").strip().lower() in ("1", "true", "yes", "on")
MAX_NAME_LEN = 64
SAFE_CHARS_RE = re.compile(r"[^0-9A-Za-z\u00C0-\uFFFF \-_.()!@#\$%&\+\=,:;]")
WRITE_LOCK = threading.Lock()
CAPTURE_QUEUE: Queue[Tuple[object, Future]] = Queue(maxsize=CAPTURE_QUEUE_MAXSIZE)
REGISTER_QUEUE: Queue[Tuple[object, Future]] = Queue(maxsize=REGISTER_QUEUE_MAXSIZE)
_capture_workers_started: bool = False
_register_workers_started: bool = False

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
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    # Give SQLite more time to wait on locks during bursts of writes.
    conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    # Enable WAL to improve concurrent read/write behavior.
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")

    pokedex_logger.info("Initialized DB")

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
ADMIN_LOG_PATH = os.path.join(LOG_DIR, "admin_audit.log")
ADMIN_USER_AGENT = os.environ.get("POKEDEX_ADMIN_UA", "PMTU-Pokedex-Admin")

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

# Dedicated audit logger for admin UA detection.
admin_audit_logger = logging.getLogger("pmtu_pokedex_admin_audit")
admin_audit_logger.setLevel(logging.INFO)
if not admin_audit_logger.handlers:
    admin_handler = RotatingFileHandler(
        ADMIN_LOG_PATH,
        maxBytes=1 * 1024 * 1024,
        backupCount=5,
    )
    admin_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
    ))
    admin_audit_logger.addHandler(admin_handler)

# Create the app.
app = FastAPI(title="PMTU Global Pokedex", root_path="/api")

@app.on_event("startup")
def startup():
    os.makedirs("/data", exist_ok=True)
    init_db()
    _start_register_workers()
    _start_capture_workers()

class RegisterReq(BaseModel):
    steam_id: str = Field(..., min_length=3, max_length=64)
    steam_name: Optional[str] = None

class CaptureReq(BaseModel):
    steam_id: str
    pokemon_name: str = Field(..., min_length=1, max_length=64)
    shiny: Optional[bool] = False
    captured_at: Optional[str] = None


def _start_register_workers():
    """Spin up background worker threads to process register writes."""
    global _register_workers_started
    if _register_workers_started:
        return

    worker_count = max(1, REGISTER_WORKERS)
    for idx in range(worker_count):
        t = threading.Thread(
            target=_register_worker,
            name=f"register-worker-{idx+1}",
            daemon=True,
        )
        t.start()
    _register_workers_started = True


def _start_capture_workers():
    """Spin up background worker threads to process capture writes."""
    global _capture_workers_started
    if _capture_workers_started:
        return

    worker_count = max(1, CAPTURE_WORKERS)
    for idx in range(worker_count):
        t = threading.Thread(
            target=_capture_worker,
            name=f"capture-worker-{idx+1}",
            daemon=True,
        )
        t.start()
    _capture_workers_started = True


def _register_worker():
    while True:
        req, fut = REGISTER_QUEUE.get()
        try:
            body, status = _process_register(req)
            fut.set_result((body, status))
        except Exception as exc:
            fut.set_exception(exc)
        finally:
            REGISTER_QUEUE.task_done()


def _process_register(req: RegisterReq):
    pokedex_logger.info(f"Registering {req.steam_name} ({req.steam_id})")

    now = datetime.now(timezone.utc).isoformat()
    safe = make_safe_name(req.steam_name)
    with WRITE_LOCK:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT steam_id FROM players WHERE steam_id=?", (req.steam_id,))
        exists = cur.fetchone() is not None
        if exists:
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

    status = 201 if not exists else 200
    return {
        "ok": True,
        "steam_id": req.steam_id,
        "steam_name": req.steam_name,
        "steam_name_safe": safe,
        "created": not exists,
        "updated": exists,
    }, status


def _capture_worker():
    while True:
        req, fut = CAPTURE_QUEUE.get()
        try:
            body, status = _process_capture(req)
            fut.set_result((body, status))
        except Exception as exc:  # Propagate any error back to the waiting request.
            fut.set_exception(exc)
        finally:
            CAPTURE_QUEUE.task_done()


def _process_capture(req: CaptureReq):
    # Ignore Mega and Gmax forms completely.
    name_lower = req.pokemon_name.lower()
    if name_lower.startswith("mega ") or name_lower.startswith("gmax "):
        return {"ok": True, "ignored": True, "reason": "mega-gmax-not-tracked"}, 200

    # Log it.
    pokedex_logger.info(
        f"{req.steam_id} captured {req.pokemon_name} {'[shiny]' if req.shiny else ''}"
    )

    # Get the current time.
    if req.captured_at is None:
        req.captured_at = datetime.now(timezone.utc).isoformat()

    with WRITE_LOCK:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT 1 FROM players WHERE steam_id=?", (req.steam_id,))
        if not cur.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Player not registered")

        # If this capture already exists (and isn’t a shiny upgrade), short-circuit.
        cur.execute(
            "SELECT shiny FROM captures WHERE steam_id=? AND pokemon_name=?",
            (req.steam_id, req.pokemon_name),
        )
        existing = cur.fetchone()
        if existing and (existing["shiny"] == 1 or not req.shiny):
            conn.close()
            return {
                "ok": True,
                "inserted": False,
                "shiny_upgraded": False,
                "first_overall": False,
                "first_shiny": False,
            }, 200

        # Snapshot counts before any writes to compute "first" flags.
        cur.execute(
            """
            SELECT
              COUNT(DISTINCT steam_id) AS total_players,
              SUM(CASE WHEN shiny = 1 THEN 1 ELSE 0 END) AS shiny_rows
            FROM captures
            WHERE pokemon_name = ?
            """,
            (req.pokemon_name,),
        )
        pre_counts = cur.fetchone()
        pre_total_players = pre_counts["total_players"] or 0
        pre_shiny = pre_counts["shiny_rows"] or 0

        # Insert if not present; ignore if already exists
        cur.execute(
            """
            INSERT OR IGNORE INTO captures(steam_id, pokemon_name, shiny, captured_at)
            VALUES(?,?,?,?)
            """,
            (req.steam_id, req.pokemon_name, 1 if req.shiny else 0, req.captured_at),
        )
        inserted = cur.rowcount > 0

        shiny_upgraded = False
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
            shiny_upgraded = cur.rowcount > 0
            if shiny_upgraded:
                pokedex_logger.info(
                    "Shiny upgrade for %s -> %s", req.steam_id, req.pokemon_name
                )
        conn.commit()
        conn.close()

    status = 201 if inserted or shiny_upgraded else 200
    first_overall = inserted and pre_total_players == 0
    first_shiny = req.shiny and pre_shiny == 0 and (inserted or shiny_upgraded)
    body = {
        "ok": True,
        "inserted": inserted,
        "shiny_upgraded": shiny_upgraded,
        "first_overall": first_overall,
        "first_shiny": first_shiny,
    }
    return body, status

class UncaptureReq(BaseModel):
    steam_id: str
    pokemon_name: str = Field(..., min_length=1, max_length=64)

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/v1/register")
def register(req: RegisterReq):
    fut: Future = Future()
    try:
        REGISTER_QUEUE.put_nowait((req, fut))
    except Full:
        raise HTTPException(
            status_code=429,
            detail="Register queue is full; try again shortly.",
        )

    if REGISTER_IMMEDIATE_ACK:
        # Return quickly and let the worker process the request.
        return JSONResponse(content={"ok": True, "queued": True}, status_code=202)

    try:
        body, status = fut.result(timeout=REGISTER_PROCESS_TIMEOUT)
        return JSONResponse(content=body, status_code=status)
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Registration is queued but still processing; please retry.",
        )
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise exc
        raise

@app.post("/v1/capture")
def capture(req: CaptureReq):
    name_lower = req.pokemon_name.lower()
    if name_lower.startswith("mega ") or name_lower.startswith("gmax "):
        return {"ok": True, "ignored": True, "reason": "mega-gmax-not-tracked"}

    # Fast path: if this capture already exists (and isn't a shiny upgrade), return immediately.
    try:
        conn = db(); cur = conn.cursor()
        cur.execute(
            "SELECT shiny FROM captures WHERE steam_id=? AND pokemon_name=?",
            (req.steam_id, req.pokemon_name),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            already_shiny = row["shiny"] == 1
            if already_shiny or not req.shiny:
                return JSONResponse(
                    content={
                        "ok": True,
                        "inserted": False,
                        "shiny_upgraded": False,
                        "first_overall": False,
                        "first_shiny": False,
                    },
                    status_code=200,
                )
            # else: fall through to queue for shiny upgrade
    except sqlite3.OperationalError:
        # If DB is momentarily busy, fall back to queue processing.
        pass

    fut: Future = Future()
    try:
        CAPTURE_QUEUE.put_nowait((req, fut))
    except Full:
        raise HTTPException(
            status_code=429,
            detail="Capture queue is full; try again shortly.",
        )

    if CAPTURE_IMMEDIATE_ACK:
        # Return quickly and let the worker process the request.
        return JSONResponse(content={"ok": True, "queued": True}, status_code=202)

    try:
        body, status = fut.result(timeout=CAPTURE_PROCESS_TIMEOUT)
        return JSONResponse(content=body, status_code=status)
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Capture is queued but still processing; please retry.",
        )
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise exc
        raise

@app.post("/v1/uncapture")
def uncapture(req: UncaptureReq):
    """Remove a capture for a player (ignores shiny flag)."""
    pokedex_logger.info(f"Uncapturing {req.pokemon_name} for {req.steam_id}")

    with WRITE_LOCK:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT 1 FROM players WHERE steam_id=?", (req.steam_id,))
        if not cur.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Player not registered")

        cur.execute(
            "DELETE FROM captures WHERE steam_id = ? AND pokemon_name = ?",
            (req.steam_id, req.pokemon_name),
        )
        deleted = cur.rowcount
        conn.commit()
        conn.close()

    return {"ok": True, "deleted": deleted}

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
def leaderboard(limit: int = 50):
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

@app.get("/v1/player/search")
def search_player(query: str = Query(..., min_length=1, max_length=64)):
    """Lookup a player by Steam ID or (sanitized) name and return captures plus rank."""
    q = query.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Query is required")

    like = f"%{q}%"
    conn = db()
    cur = conn.cursor()

    # Grab the player with totals first.
    cur.execute(
        """
        SELECT p.steam_id,
               p.steam_name,
               p.steam_name_safe,
               COUNT(c.id) AS total,
               SUM(CASE WHEN c.shiny = 1 THEN 1 ELSE 0 END) AS shinies
        FROM players p
        LEFT JOIN captures c ON p.steam_id = c.steam_id
        WHERE p.steam_id = ?
           OR p.steam_name_safe LIKE ?
        GROUP BY p.steam_id
        ORDER BY total DESC, shinies DESC, p.steam_name COLLATE NOCASE
        LIMIT 1
        """,
        (q, like),
    )
    player = cur.fetchone()
    if not player:
        conn.close()
        raise HTTPException(status_code=404, detail="Player not found")

    steam_id = player["steam_id"]
    safe_name = player["steam_name_safe"] or player["steam_name"] or "Unknown"
    total_captures = player["total"] or 0
    shinies = player["shinies"] or 0

    # Compute rank using the same ordering rules as the leaderboard.
    cur.execute(
        """
        SELECT
          1 + COUNT(*) AS rank
        FROM (
          SELECT
            p.steam_id,
            COUNT(c.id) AS total,
            SUM(CASE WHEN c.shiny = 1 THEN 1 ELSE 0 END) AS shinies,
            COALESCE(p.steam_name_safe, p.steam_name, 'Unknown') AS name_key
          FROM players p
          LEFT JOIN captures c ON p.steam_id = c.steam_id
          GROUP BY p.steam_id
        ) lb
        WHERE lb.total > ?
           OR (lb.total = ? AND lb.shinies > ?)
           OR (lb.total = ? AND lb.shinies = ? AND lb.name_key COLLATE NOCASE < ?)
        """,
        (
            total_captures,
            total_captures,
            shinies,
            total_captures,
            shinies,
            safe_name,
        ),
    )
    rank_row = cur.fetchone()
    rank = rank_row["rank"] if rank_row else 1

    # Fetch captures for display.
    cur.execute(
        """
        SELECT pokemon_name, shiny, captured_at
        FROM captures
        WHERE steam_id = ?
        ORDER BY pokemon_name COLLATE NOCASE
        """,
        (steam_id,),
    )
    captures = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "steam_id": steam_id,
        "steam_name": player["steam_name"],
        "steam_name_safe": player["steam_name_safe"],
        "total": total_captures,
        "shinies": shinies,
        "rank": rank,
        "captures": captures,
    }

@app.get("/v1/species/{pokemon_name}/caught")
def caught_count(pokemon_name: str):
    pokedex_logger.info(f"Getting caught count for {pokemon_name}")

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

@app.get("/v1/species/search")
def search_species(term: str = Query("", max_length=64), limit: int = 15):
    """Autocomplete for Pokémon names seen in captures only."""
    term = term.strip()
    pattern = f"%{term}%" if term else "%"
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT pokemon_name
        FROM captures
        WHERE pokemon_name LIKE ?
        ORDER BY pokemon_name COLLATE NOCASE
        LIMIT ?
        """,
        (pattern, limit),
    )
    names = [r["pokemon_name"] for r in cur.fetchall()]
    conn.close()
    return {"names": names}

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
    method = request.method
    path = request.url.path
    ua = request.headers.get("user-agent", "-")
    client_ip = request.client.host if request.client else "-"

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
            "%s %s %d %.2fms UA=%s",
            method,
            path,
            status,
            duration_ms,
            ua,
        )

        # Detect admin user agent and log to dedicated audit file without blocking the request.
        if ua and ua.strip().lower() == ADMIN_USER_AGENT.strip().lower():
            admin_audit_logger.info(
                "ADMIN UA %s %s?%s %d %.2fms ip=%s",
                method,
                path,
                request.url.query or "",
                status,
                duration_ms,
                client_ip,
            )

    return response
