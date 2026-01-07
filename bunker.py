# attendance_app.py
# Streamlit 1.52.2 compatible
# DB: attendance.db
#
# FEATURES (per your spec):
# 1) Auth: username+password (case-insensitive username uniqueness, DB enforced)
# 2) Users only see their personal tracker(s). Global timetable is hidden.
#    - On first login, user automatically gets a personal editable copy of the global timetable.
# 3) Attendance is per-user:
#    sessions UNIQUE(user_id, class_id, session_date)
# 4) Prompts show ALL pending prompts up to now (not just today), respecting end_time + buffer.
# 5) Undo last attendance action (and "Modify past attendance" tool).
# 6) Conventional button labels.
#
# Notes:
# - This is still "no-auth-provider" auth. Passwords hashed (PBKDF2-HMAC-SHA256).
# - Streamlit Cloud is stateless across sessions, but DB persists inside the app storage.
# - If you redeploy and DB resets, users will need to re-signup (unless you persist the DB file).

import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import streamlit as st

DB = "attendance.db"

GLOBAL_TRACKER_NAME = "Sem 6 - NITT EEE B"
POST_CLASS_BUFFER_MIN = 5  # show prompt after end time + buffer
DEFAULT_RANGE_DAYS = 150   # default tracker range if creating global fresh

IST = timezone(timedelta(hours=5, minutes=30))

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_TO_INT = {d: i for i, d in enumerate(DAYS)}

TRACKER_PALETTE = [
    "#1abc9c", "#3498db", "#9b59b6", "#e67e22", "#e84393",
    "#16a085", "#2980b9", "#8e44ad", "#c0392b", "#2c3e50",
]
COURSE_PALETTE = [
    "#2ecc71", "#e74c3c", "#f1c40f", "#9b59b6", "#3498db",
    "#1abc9c", "#e67e22", "#34495e", "#d35400", "#8e44ad",
]


# -------------------- DB --------------------
def conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _table_exists(c: sqlite3.Connection, table: str) -> bool:
    r = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return r is not None


def _has_column(c: sqlite3.Connection, table: str, col: str) -> bool:
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)


def init_db():
    """
    Schema:
      users:
        - username_lower UNIQUE (case-insensitive collision prevention)
        - username_display (as entered)
        - password_hash (pbkdf2)
      trackers:
        - is_global (0/1)
        - owner_user_id NULL for global
        - cloned_from tracker_id for user clones
        - UNIQUE(owner_user_id, cloned_from) for single clone per user
      sessions:
        - user_id scoped UNIQUE(user_id, class_id, session_date)

    Migration:
      - if older tables exist, we add/migrate safely.
      - legacy sessions without user_id are assigned to owner_user_id='OWNER_LEGACY' (so they don’t leak).
    """
    with conn() as c:
        # Core tables
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username_display TEXT NOT NULL,
                username_lower TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trackers (
                tracker_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS classes (
                class_id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                day_of_week INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                tracker_id INTEGER NOT NULL
            );
            """
        )      
        
        
        # ---- users table migration (schema drift fix) ----
        if not _has_column(c, "users", "username_display"):
            c.execute("ALTER TABLE users ADD COLUMN username_display TEXT")

        # Backfill display name for existing users
        c.execute("""
            UPDATE users
            SET username_display = username_lower
            WHERE username_display IS NULL
        """)

        

        # Trackers migration fields
        if not _has_column(c, "trackers", "is_global"):
            c.execute("ALTER TABLE trackers ADD COLUMN is_global INTEGER DEFAULT 0")
        if not _has_column(c, "trackers", "owner_user_id"):
            c.execute("ALTER TABLE trackers ADD COLUMN owner_user_id INTEGER")
        if not _has_column(c, "trackers", "cloned_from"):
            c.execute("ALTER TABLE trackers ADD COLUMN cloned_from INTEGER")

        # Sessions table migration
        if not _table_exists(c, "sessions"):
            c.executescript(
                """
                CREATE TABLE sessions (
                    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    class_id INTEGER NOT NULL,
                    session_date TEXT NOT NULL,
                    status TEXT CHECK(status IN ('PENDING','ATTENDED','MISSED','CANCELLED')) NOT NULL,
                    UNIQUE(user_id, class_id, session_date)
                );
                """
            )
        else:
            # if sessions exists but is old format (no user_id), rebuild
            if not _has_column(c, "sessions", "user_id"):
                # assign legacy sessions to synthetic user OWNER_LEGACY (id stored in app_meta)
                legacy_uid = c.execute(
                    "SELECT value FROM app_meta WHERE key='legacy_user_id'"
                ).fetchone()
                if legacy_uid is None:
                    # create legacy user row
                    # username_lower unique; use fixed.
                    created = datetime.now(IST).isoformat(timespec="seconds")
                    pw_hash = _hash_password("legacy", _new_salt())
                    c.execute(
                        "INSERT OR IGNORE INTO users(username_display, username_lower, password_hash, created_at) VALUES (?,?,?,?)",
                        ("OWNER_LEGACY", "owner_legacy", pw_hash, created),
                    )
                    legacy_row = c.execute(
                        "SELECT user_id FROM users WHERE username_lower='owner_legacy'"
                    ).fetchone()
                    legacy_id = int(legacy_row["user_id"])
                    c.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('legacy_user_id', ?)", (str(legacy_id),))
                else:
                    legacy_id = int(legacy_uid["value"])

                # rename and rebuild
                c.execute("ALTER TABLE sessions RENAME TO sessions_old")
                c.executescript(
                    """
                    CREATE TABLE sessions (
                        session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        class_id INTEGER NOT NULL,
                        session_date TEXT NOT NULL,
                        status TEXT CHECK(status IN ('PENDING','ATTENDED','MISSED','CANCELLED')) NOT NULL,
                        UNIQUE(user_id, class_id, session_date)
                    );
                    """
                )
                # sessions_old likely: session_id, class_id, session_date, status
                # copy into new with legacy user_id
                c.execute(
                    """
                    INSERT INTO sessions(session_id, user_id, class_id, session_date, status)
                    SELECT session_id, ?, class_id, session_date, status
                    FROM sessions_old
                    """,
                    (legacy_id,),
                )
                c.execute("DROP TABLE sessions_old")
            else:
                # Ensure uniqueness index exists
                try:
                    c.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_user_class_date ON sessions(user_id, class_id, session_date)"
                    )
                except Exception:
                    pass

        # Clone-guard index (single clone per user per global)
        try:
            c.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_clone
                ON trackers(owner_user_id, cloned_from)
                WHERE cloned_from IS NOT NULL
                """
            )
        except Exception:
            # old sqlite may not support partial index; we still guard in code.
            pass

        # Ensure exactly one GLOBAL tracker exists and is named
        global_t = c.execute(
            "SELECT * FROM trackers WHERE is_global=1 ORDER BY tracker_id LIMIT 1"
        ).fetchone()

        if global_t is None:
            # Promote first tracker if exists, else create
            first = c.execute("SELECT * FROM trackers ORDER BY tracker_id LIMIT 1").fetchone()
            if first is None:
                c.execute(
                    """
                    INSERT INTO trackers(name, created_at, start_date, end_date, is_global, owner_user_id, cloned_from)
                    VALUES (?,?,?,?,1,NULL,NULL)
                    """,
                    (
                        GLOBAL_TRACKER_NAME,
                        datetime.now(IST).isoformat(timespec="seconds"),
                        date.today().isoformat(),
                        (date.today() + timedelta(days=DEFAULT_RANGE_DAYS)).isoformat(),
                    ),
                )
            else:
                c.execute(
                    """
                    UPDATE trackers
                    SET name=?, is_global=1, owner_user_id=NULL, cloned_from=NULL
                    WHERE tracker_id=?
                    """,
                    (GLOBAL_TRACKER_NAME, int(first["tracker_id"])),
                )
        else:
            c.execute(
                """
                UPDATE trackers
                SET name=?, is_global=1, owner_user_id=NULL, cloned_from=NULL
                WHERE tracker_id=?
                """,
                (GLOBAL_TRACKER_NAME, int(global_t["tracker_id"])),
            )

        # Demote extra globals if any
        globals_all = c.execute(
            "SELECT tracker_id FROM trackers WHERE is_global=1 ORDER BY tracker_id"
        ).fetchall()
        if len(globals_all) > 1:
            keep = int(globals_all[0]["tracker_id"])
            c.execute("UPDATE trackers SET is_global=0 WHERE is_global=1 AND tracker_id<>?", (keep,))

        c.commit()


# -------------------- Password hashing --------------------
def _new_salt() -> str:
    return secrets.token_hex(16)


def _hash_password(password: str, salt: str) -> str:
    # pbkdf2_hmac: good enough for this app (no external deps)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256$200000${salt}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    # stored: pbkdf2_sha256$iters$salt$hash
    try:
        scheme, iters_s, salt, hexhash = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iters)
        return secrets.compare_digest(dk.hex(), hexhash)
    except Exception:
        return False


def normalize_username(u: str) -> str:
    # case-insensitive uniqueness: store lower + strip
    return (u or "").strip().lower()


# -------------------- Helpers --------------------
def parse_time_to_minutes(hhmm: str) -> int:
    t = datetime.strptime(hhmm.strip(), "%H:%M").time()
    return t.hour * 60 + t.minute


def normalize_time(hhmm: str) -> str:
    t = datetime.strptime(hhmm.strip(), "%H:%M").time()
    return f"{t.hour:02d}:{t.minute:02d}"


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def stable_color(seed: str, palette: List[str]) -> str:
    h = 0
    for ch in seed:
        h = (h * 131 + ord(ch)) % 10_000_000
    return palette[h % len(palette)]


def inject_css():
    st.markdown(
        """
        <style>
          .tracker-blob {
            border-radius: 18px;
            padding: 16px;
            margin-bottom: 14px;
            color: #ffffff;
            box-shadow: 0 6px 16px rgba(0,0,0,0.12);
          }
          .tracker-blob h4 { margin: 0 0 6px 0; font-weight: 900; }
          .tracker-meta { opacity: 0.95; font-size: 0.90rem; }

          .fab { position: fixed; right: 24px; bottom: 24px; z-index: 10000; }
          .fab a {
            display: inline-flex; align-items: center; justify-content: center;
            width: 56px; height: 56px; border-radius: 50%;
            font-size: 32px; text-decoration: none;
            background: #f1c40f; color: #111;
            box-shadow: 0 6px 16px rgba(0,0,0,0.35);
            user-select: none;
          }

          .dayhead { font-weight: 900; margin-bottom: 10px; }
          .time-axis { font-size: 0.82rem; opacity: 0.70; padding-top: 8px; white-space: nowrap; }

          .day-box {
            border: 1px solid rgba(255,255,255,0.10);
            border-radius: 14px;
            padding: 10px;
            min-height: 60px;
            background: rgba(255,255,255,0.02);
          }

          .band-cell { display: flex; flex-direction: column; justify-content: flex-end; gap: 10px; }

          .course-pill {
            border-radius: 14px;
            padding: 10px 12px;
            color: #fff;
            font-weight: 850;
            border: 1px solid rgba(255,255,255,0.18);
            box-shadow: 0 4px 10px rgba(0,0,0,0.10);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
          }
          .pill-meta { font-weight: 650; font-size: 0.82rem; opacity: 0.95; margin-top: 4px; }
          .pill-status {
            margin-top: 10px; font-size: 0.76rem; padding: 1px 10px; border-radius: 999px;
            background: rgba(0,0,0,0.18); display: inline-block; width: fit-content;
          }

          .range-note { opacity: 0.75; font-size: 0.90rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# -------------------- Auth data access --------------------
def user_create(username_display: str, password: str) -> int:
    uname_lower = normalize_username(username_display)
    if not uname_lower:
        raise ValueError("Username cannot be empty.")
    if len(password) < 4:
        raise ValueError("Password must be at least 4 characters.")

    salt = _new_salt()
    pw_hash = _hash_password(password, salt)
    created = datetime.now(IST).isoformat(timespec="seconds")

    with conn() as c:
        # UNIQUE(username_lower) prevents collisions, case-insensitive
        c.execute(
            "INSERT INTO users(username_display, username_lower, password_hash, created_at) VALUES (?,?,?,?)",
            ((username_display or "").strip(), uname_lower, pw_hash, created),
        )
        uid = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        return uid


def user_authenticate(username: str, password: str) -> Optional[int]:
    uname_lower = normalize_username(username)
    if not uname_lower:
        return None
    with conn() as c:
        u = c.execute(
            "SELECT user_id, password_hash FROM users WHERE username_lower=?",
            (uname_lower,),
        ).fetchone()
        if not u:
            return None
        if not _verify_password(password, u["password_hash"]):
            return None
        return int(u["user_id"])


def get_user_display(user_id: int) -> str:
    with conn() as c:
        u = c.execute("SELECT username_display FROM users WHERE user_id=?", (user_id,)).fetchone()
        return (u["username_display"] if u else "User")


# -------------------- Tracker & clone model --------------------
def get_global_tracker() -> sqlite3.Row:
    with conn() as c:
        t = c.execute(
            "SELECT * FROM trackers WHERE is_global=1 ORDER BY tracker_id LIMIT 1"
        ).fetchone()
        if not t:
            raise RuntimeError("Global tracker missing.")
        return t


def get_or_create_user_clone(user_id: int) -> int:
    """
    Users must only ever interact with a copy of global timetable.
    This returns the user's clone tracker_id. It creates it if missing.
    """
    g = get_global_tracker()
    gid = int(g["tracker_id"])

    with conn() as c:
        existing = c.execute(
            """
            SELECT tracker_id FROM trackers
            WHERE owner_user_id=? AND cloned_from=?
            ORDER BY tracker_id LIMIT 1
            """,
            (user_id, gid),
        ).fetchone()
        if existing:
            return int(existing["tracker_id"])

        # Create clone tracker
        c.execute(
            """
            INSERT INTO trackers(name, created_at, start_date, end_date, is_global, owner_user_id, cloned_from)
            VALUES (?,?,?,?,0,?,?)
            """,
            (
                g["name"],
                datetime.now(IST).isoformat(timespec="seconds"),
                g["start_date"],
                g["end_date"],
                user_id,
                gid,
            ),
        )
        new_tid = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])

        # Copy timetable classes ONLY (no sessions copied)
        classes = c.execute(
            "SELECT subject, day_of_week, start_time, end_time FROM classes WHERE tracker_id=? ORDER BY class_id",
            (gid,),
        ).fetchall()
        for cl in classes:
            c.execute(
                "INSERT INTO classes(subject, day_of_week, start_time, end_time, tracker_id) VALUES (?,?,?,?,?)",
                (cl["subject"], int(cl["day_of_week"]), cl["start_time"], cl["end_time"], new_tid),
            )
        return new_tid


def list_user_trackers(user_id: int) -> List[sqlite3.Row]:
    """
    USER ONLY: do not show global tracker at all.
    Always include user's clone of global (auto-created).
    """
    clone_id = get_or_create_user_clone(user_id)
    with conn() as c:
        # user-owned trackers only
        return c.execute(
            """
            SELECT * FROM trackers
            WHERE owner_user_id=?
            ORDER BY tracker_id
            """,
            (user_id,),
        ).fetchall()


def get_tracker_for_user(user_id: int, tracker_id: int) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            """
            SELECT * FROM trackers
            WHERE tracker_id=? AND owner_user_id=?
            """,
            (tracker_id, user_id),
        ).fetchone()


def create_tracker_for_user(user_id: int, name: str, start_date: date, end_date: date):
    nm = (name or "").strip() or "Untitled Tracker"
    if end_date < start_date:
        raise ValueError("End date must be on/after start date.")
    with conn() as c:
        c.execute(
            """
            INSERT INTO trackers(name, created_at, start_date, end_date, is_global, owner_user_id, cloned_from)
            VALUES (?,?,?,?,0,?,NULL)
            """,
            (nm, datetime.now(IST).isoformat(timespec="seconds"), start_date.isoformat(), end_date.isoformat(), user_id),
        )


# -------------------- Classes & sessions --------------------
def list_classes(tracker_id: int) -> List[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            """
            SELECT * FROM classes
            WHERE tracker_id=?
            ORDER BY day_of_week, start_time, end_time, subject
            """,
            (tracker_id,),
        ).fetchall()


def add_class(tracker_id: int, subject: str, day: int, start: str, end: str) -> int:
    subj = (subject or "").strip()
    if not subj:
        raise ValueError("Course cannot be empty.")

    start_n = normalize_time(start)
    end_n = normalize_time(end)
    if parse_time_to_minutes(end_n) <= parse_time_to_minutes(start_n):
        raise ValueError("End time must be after start time.")

    with conn() as c:
        c.execute(
            "INSERT INTO classes(subject, day_of_week, start_time, end_time, tracker_id) VALUES (?,?,?,?,?)",
            (subj, day, start_n, end_n, tracker_id),
        )
        return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])


def update_class(class_id: int, subject: str, day: int, start: str, end: str) -> Optional[Dict]:
    subj = (subject or "").strip()
    if not subj:
        raise ValueError("Course cannot be empty.")

    start_n = normalize_time(start)
    end_n = normalize_time(end)
    if parse_time_to_minutes(end_n) <= parse_time_to_minutes(start_n):
        raise ValueError("End time must be after start time.")

    with conn() as c:
        old = c.execute(
            "SELECT class_id, subject, day_of_week, start_time, end_time, tracker_id FROM classes WHERE class_id=?",
            (class_id,),
        ).fetchone()
        c.execute(
            "UPDATE classes SET subject=?, day_of_week=?, start_time=?, end_time=? WHERE class_id=?",
            (subj, day, start_n, end_n, class_id),
        )
    return dict(old) if old else None


def delete_class(class_id: int) -> Optional[Dict]:
    with conn() as c:
        old = c.execute(
            "SELECT class_id, subject, day_of_week, start_time, end_time, tracker_id FROM classes WHERE class_id=?",
            (class_id,),
        ).fetchone()
        c.execute("DELETE FROM sessions WHERE class_id=?", (class_id,))
        c.execute("DELETE FROM classes WHERE class_id=?", (class_id,))
    return dict(old) if old else None


def set_status(session_id: int, status: str):
    with conn() as c:
        c.execute("UPDATE sessions SET status=? WHERE session_id=?", (status, session_id))


def ensure_sessions_for_week(user_id: int, tracker_id: int, week_start: date, tracker_start: date, tracker_end: date):
    """
    Create sessions for the given week for THIS user.
    """
    classes = list_classes(tracker_id)
    with conn() as c:
        for cl in classes:
            d = week_start + timedelta(days=int(cl["day_of_week"]))
            if d < tracker_start or d > tracker_end:
                continue
            c.execute(
                """
                INSERT OR IGNORE INTO sessions(user_id, class_id, session_date, status)
                VALUES (?,?,?, 'PENDING')
                """,
                (user_id, int(cl["class_id"]), d.isoformat()),
            )


def ensure_sessions_up_to_today(user_id: int, tracker_id: int, tracker_start: date, tracker_end: date):
    """
    For "pending backlog prompts", we must ensure sessions exist from tracker_start up to today.
    We generate per-week (cheap inserts with IGNORE).
    """
    today = min(date.today(), tracker_end)
    if today < tracker_start:
        return
    start_monday = monday_of(tracker_start)
    end_monday = monday_of(today)
    w = start_monday
    while w <= end_monday:
        ensure_sessions_for_week(user_id, tracker_id, w, tracker_start, tracker_end)
        w += timedelta(days=7)


def get_sessions_for_week(user_id: int, tracker_id: int, week_start: date) -> List[sqlite3.Row]:
    dates = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]
    with conn() as c:
        return c.execute(
            f"""
            SELECT s.session_id, s.session_date, s.status,
                   c.subject, c.start_time, c.end_time, c.class_id
            FROM sessions s
            JOIN classes c ON c.class_id=s.class_id
            WHERE s.user_id=?
              AND c.tracker_id=?
              AND s.session_date IN ({','.join('?'*7)})
            ORDER BY s.session_date, c.start_time, c.end_time, c.subject
            """,
            (user_id, tracker_id, *dates),
        ).fetchall()


def get_pending_prompts_up_to_now(user_id: int, tracker_id: int, tracker_start: date, tracker_end: date) -> List[sqlite3.Row]:
    """
    Return all sessions up to today that are still PENDING,
    and only those where (session_end_time + buffer) <= now(IST).
    """
    ensure_sessions_up_to_today(user_id, tracker_id, tracker_start, tracker_end)

    now = datetime.now(IST)
    today = min(date.today(), tracker_end)

    with conn() as c:
        # fetch pending sessions up to today
        rows = c.execute(
            """
            SELECT s.session_id, s.session_date, s.status,
                   c.subject, c.start_time, c.end_time
            FROM sessions s
            JOIN classes c ON c.class_id=s.class_id
            WHERE s.user_id=?
              AND c.tracker_id=?
              AND s.status='PENDING'
              AND s.session_date <= ?
            ORDER BY s.session_date ASC, c.end_time ASC, c.start_time ASC, c.subject ASC
            """,
            (user_id, tracker_id, today.isoformat()),
        ).fetchall()

    # filter by end_time + buffer
    out: List[sqlite3.Row] = []
    for r in rows:
        d = date.fromisoformat(r["session_date"])
        end_t = datetime.strptime(r["end_time"], "%H:%M").time()
        end_dt = datetime.combine(d, end_t, tzinfo=IST) + timedelta(minutes=POST_CLASS_BUFFER_MIN)
        if now >= end_dt:
            out.append(r)
    return out


def course_stats(user_id: int, tracker_id: int) -> List[Dict]:
    with conn() as c:
        rows = c.execute(
            """
            SELECT c.subject,
                   SUM(s.status='ATTENDED') AS a,
                   SUM(s.status='MISSED')   AS m,
                   SUM(s.status='CANCELLED') AS cx
            FROM sessions s
            JOIN classes c ON c.class_id=s.class_id
            WHERE s.user_id=? AND c.tracker_id=?
            GROUP BY c.subject
            ORDER BY c.subject
            """,
            (user_id, tracker_id),
        ).fetchall()

    out: List[Dict] = []
    for r in rows:
        a = r["a"] or 0
        m = r["m"] or 0
        cx = r["cx"] or 0
        denom = a + m
        pct = (a / denom * 100.0) if denom else 0.0
        out.append({"Course": r["subject"], "Attended": a, "Missed": m, "Cancelled": cx, "Pct": round(pct, 2)})
    return out


def clear_timetable(tracker_id: int):
    with conn() as c:
        ids = [r["class_id"] for r in c.execute("SELECT class_id FROM classes WHERE tracker_id=?", (tracker_id,)).fetchall()]
        if ids:
            q = ",".join(["?"] * len(ids))
            c.execute(f"DELETE FROM sessions WHERE class_id IN ({q})", ids)
        c.execute("DELETE FROM classes WHERE tracker_id=?", (tracker_id,))


def delete_tracker(tracker_id: int):
    clear_timetable(tracker_id)
    with conn() as c:
        c.execute("DELETE FROM trackers WHERE tracker_id=?", (tracker_id,))


def apply_undo_timetable(action: Dict):
    typ = action.get("type")
    if typ == "add":
        cid = int(action["class_id"])
        with conn() as c:
            c.execute("DELETE FROM sessions WHERE class_id=?", (cid,))
            c.execute("DELETE FROM classes WHERE class_id=?", (cid,))
        return

    if typ == "edit":
        cid = int(action["class_id"])
        old = action.get("old") or {}
        with conn() as c:
            c.execute(
                "UPDATE classes SET subject=?, day_of_week=?, start_time=?, end_time=? WHERE class_id=?",
                (old.get("subject"), int(old.get("day_of_week")), old.get("start_time"), old.get("end_time"), cid),
            )
        return

    if typ == "delete":
        old = action.get("old") or {}
        with conn() as c:
            c.execute(
                "INSERT INTO classes(class_id, subject, day_of_week, start_time, end_time, tracker_id) VALUES (?,?,?,?,?,?)",
                (
                    int(old.get("class_id")),
                    old.get("subject"),
                    int(old.get("day_of_week")),
                    old.get("start_time"),
                    old.get("end_time"),
                    int(old.get("tracker_id")),
                ),
            )
        return

    raise ValueError("Unsupported undo type.")


# -------------------- UI: dashboards & layout --------------------
def render_course_dashboard(user_id: int, tracker_id: int):
    stats = course_stats(user_id, tracker_id)
    if not stats:
        st.info("No attendance data yet for this tracker.")
        return

    try:
        import plotly.graph_objects as go  # type: ignore
    except Exception:
        go = None

    cards_per_row = 4
    for i in range(0, len(stats), cards_per_row):
        row = stats[i : i + cards_per_row]
        cols = st.columns(cards_per_row)
        for j, item in enumerate(row):
            with cols[j]:
                course = item["Course"]
                pct = float(item["Pct"])
                st.markdown(f"**{course}**")
                if go is not None:
                    fig = go.Figure(
                        go.Indicator(
                            mode="gauge+number",
                            value=pct,
                            number={"suffix": "%", "valueformat": ".2f"},
                            gauge={"axis": {"range": [0, 100]}, "bar": {"thickness": 0.35}},
                        )
                    )
                    fig.update_layout(height=150, margin=dict(l=6, r=6, t=6, b=6))
                    st.plotly_chart(fig, use_container_width=True, key=f"gauge_{tracker_id}_{user_id}_{course}")
                else:
                    st.metric("Attendance", f"{pct:.2f}%")

                st.caption(f"Attended: {item['Attended']}  •  Missed: {item['Missed']}  •  Cancelled: {item['Cancelled']}")


def _duration_to_height_px(duration_min: int) -> int:
    base = 64
    px_per_min = 1.2
    h = int(base + duration_min * px_per_min)
    return max(70, min(h, 190))


def render_week_view(user_id: int, tracker_id: int, tracker_start: date, tracker_end: date) -> List[sqlite3.Row]:
    # clamp week offset
    if "week_offset" not in st.session_state:
        st.session_state.week_offset = 0

    # determine actual week start
    today = date.today()
    base_monday = monday_of(today)
    week_start = base_monday + timedelta(days=st.session_state.week_offset * 7)

    earliest = monday_of(tracker_start)
    latest = monday_of(tracker_end)

    if week_start < earliest:
        week_start = earliest
        st.session_state.week_offset = (earliest - base_monday).days // 7
    if week_start > latest:
        week_start = latest
        st.session_state.week_offset = (latest - base_monday).days // 7

    can_prev = (week_start - timedelta(days=7)) >= earliest
    can_next = (week_start + timedelta(days=7)) <= latest

    nav = st.columns([1, 6, 1])
    if nav[0].button("Previous Week", disabled=not can_prev):
        st.session_state.week_offset -= 1
        st.rerun()
    if nav[2].button("Next Week", disabled=not can_next):
        st.session_state.week_offset += 1
        st.rerun()

    # ensure sessions for this week exist
    ensure_sessions_for_week(user_id, tracker_id, week_start, tracker_start, tracker_end)
    sessions = get_sessions_for_week(user_id, tracker_id, week_start)

    st.subheader("Week View")
    st.markdown(
        f"<div class='range-note'>Tracker range: <b>{tracker_start.isoformat()}</b> → <b>{tracker_end.isoformat()}</b></div>",
        unsafe_allow_html=True,
    )

    # normalize and band by end time
    normalized = []
    for s in sessions:
        stt = normalize_time(s["start_time"])
        ent = normalize_time(s["end_time"])
        sd = date.fromisoformat(s["session_date"])
        weekday = sd.weekday()
        start_min = parse_time_to_minutes(stt)
        end_min = parse_time_to_minutes(ent)
        normalized.append(
            {
                "row": s,
                "weekday": weekday,
                "start_str": stt,
                "end_str": ent,
                "start_min": start_min,
                "end_min": end_min,
                "duration": max(1, end_min - start_min),
            }
        )

    bands: Dict[int, Dict] = {}
    for it in normalized:
        end_min = it["end_min"]
        bands.setdefault(end_min, {"end_min": end_min, "end_str": it["end_str"], "items": []})
        bands[end_min]["items"].append(it)

    band_list = sorted(bands.values(), key=lambda b: b["end_min"])

    # header row
    header_cols = st.columns([1.2] + [1] * 7)
    header_cols[0].markdown(" ")
    for i in range(7):
        d = week_start + timedelta(days=i)
        if d < tracker_start or d > tracker_end:
            header_cols[i + 1].markdown(" ")
        else:
            header_cols[i + 1].markdown(
                f"<div class='dayhead'>{DAYS[i]} • {d.strftime('%d %b')}</div>",
                unsafe_allow_html=True,
            )

    # render bands
    for band in band_list:
        items = band["items"]
        end_str = band["end_str"]

        max_h = 0
        for it in items:
            max_h = max(max_h, _duration_to_height_px(it["duration"]))

        row_cols = st.columns([1.2] + [1] * 7)
        row_cols[0].markdown(f"<div class='time-axis'>Ends {end_str}</div>", unsafe_allow_html=True)

        for day_idx in range(7):
            d = week_start + timedelta(days=day_idx)
            if d < tracker_start or d > tracker_end:
                row_cols[day_idx + 1].markdown(" ")
                continue

            day_items = [it for it in items if it["weekday"] == day_idx]
            if not day_items:
                row_cols[day_idx + 1].markdown(
                    f"<div class='day-box' style='height:{max_h}px'></div>",
                    unsafe_allow_html=True,
                )
                continue

            day_items.sort(key=lambda it: (it["start_min"], it["duration"], it["row"]["subject"]))

            blocks = []
            for it in day_items:
                s = it["row"]
                course = s["subject"]
                color = stable_color(course, COURSE_PALETTE)
                h = _duration_to_height_px(it["duration"])
                blocks.append(
                    f"""
                    <div class='course-pill' style='background:{color}; height:{h}px'>
                      <div>{course}</div>
                      <div class='pill-meta'>{it["start_str"]}–{it["end_str"]}</div>
                      <div class='pill-status'>{s["status"]}</div>
                    </div>
                    """
                )

            html = f"<div class='day-box band-cell' style='height:{max_h}px'>" + "".join(blocks) + "</div>"
            row_cols[day_idx + 1].markdown(html, unsafe_allow_html=True)

    return sessions


# -------------------- UI: prompts + undo --------------------
def render_attendance_prompts(user_id: int, tracker_id: int, tracker_start: date, tracker_end: date):
    st.subheader("Attendance Prompts")

    # Undo last attendance change
    if "last_attendance_action" not in st.session_state:
        st.session_state.last_attendance_action = None

    last = st.session_state.last_attendance_action
    if last is not None:
        c1, c2 = st.columns([2, 8])
        if c1.button("Undo Last Attendance Change"):
            set_status(int(last["session_id"]), last["prev_status"])
            st.session_state.last_attendance_action = None
            st.rerun()
        c2.caption("Reverts your most recent attendance update (within this session).")

    pending = get_pending_prompts_up_to_now(user_id, tracker_id, tracker_start, tracker_end)

    if not pending:
        st.caption("No pending prompts right now.")
        return

    # show oldest first; require user to answer all pending up to now (as they appear)
    for r in pending:
        session_day = date.fromisoformat(r["session_date"])
        label = f"{session_day.strftime('%d %b %Y')} • {r['subject']} ({r['start_time']}-{r['end_time']})"
        st.markdown(f"**{label}**")

        col1, col2, col3 = st.columns(3)
        if col1.button("Mark Attended", key=f"att_{r['session_id']}"):
            st.session_state.last_attendance_action = {"session_id": int(r["session_id"]), "prev_status": r["status"]}
            set_status(int(r["session_id"]), "ATTENDED")
            st.rerun()
        if col2.button("Mark Cancelled", key=f"can_{r['session_id']}"):
            st.session_state.last_attendance_action = {"session_id": int(r["session_id"]), "prev_status": r["status"]}
            set_status(int(r["session_id"]), "CANCELLED")
            st.rerun()
        if col3.button("Mark Missed", key=f"mis_{r['session_id']}"):
            st.session_state.last_attendance_action = {"session_id": int(r["session_id"]), "prev_status": r["status"]}
            set_status(int(r["session_id"]), "MISSED")
            st.rerun()

        st.markdown("---")


def render_modify_past_attendance(user_id: int, tracker_id: int):
    """
    Safety valve for misclicks beyond 'undo last action':
    Allows changing any recent session status for this tracker.
    """
    with st.expander("Modify Past Attendance", expanded=False):
        days_back = st.number_input("Look back (days)", min_value=1, max_value=120, value=14, step=1)
        since = (date.today() - timedelta(days=int(days_back))).isoformat()
        upto = date.today().isoformat()

        with conn() as c:
            rows = c.execute(
                """
                SELECT s.session_id, s.session_date, s.status,
                       c.subject, c.start_time, c.end_time
                FROM sessions s
                JOIN classes c ON c.class_id=s.class_id
                WHERE s.user_id=?
                  AND c.tracker_id=?
                  AND s.session_date BETWEEN ? AND ?
                ORDER BY s.session_date DESC, c.start_time ASC, c.subject ASC
                """,
                (user_id, tracker_id, since, upto),
            ).fetchall()

        if not rows:
            st.caption("No sessions found in this range.")
            return

        options = {}
        for r in rows:
            d = date.fromisoformat(r["session_date"]).strftime("%d %b %Y")
            k = f"{d} • {r['subject']} ({r['start_time']}-{r['end_time']}) • current={r['status']} • id={r['session_id']}"
            options[k] = int(r["session_id"])

        pick = st.selectbox("Select a session", list(options.keys()))
        session_id = options[pick]
        new_status = st.selectbox("Set status to", ["PENDING", "ATTENDED", "MISSED", "CANCELLED"])
        if st.button("Apply Status Change"):
            # capture prev status for undo (within session)
            # fetch current
            with conn() as c:
                cur = c.execute("SELECT status FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            prev = cur["status"] if cur else "PENDING"
            st.session_state.last_attendance_action = {"session_id": session_id, "prev_status": prev}
            set_status(session_id, new_status)
            st.success("Updated.")
            st.rerun()


# -------------------- Sidebar editor (timetable) --------------------
def sidebar_editor(tracker_id: int):
    classes = list_classes(tracker_id)

    with st.sidebar:
        st.header("Timetable Editor")

        if "undo_timetable" not in st.session_state:
            st.session_state.undo_timetable = None
        if "confirm_clear" not in st.session_state:
            st.session_state.confirm_clear = False
        if "confirm_delete" not in st.session_state:
            st.session_state.confirm_delete = False

        # Undo timetable change
        if st.button("Undo Last Timetable Change", disabled=not bool(st.session_state.undo_timetable)):
            try:
                apply_undo_timetable(st.session_state.undo_timetable)
                st.session_state.undo_timetable = None
                st.rerun()
            except Exception as e:
                st.error(f"Undo failed: {e}")

        st.divider()

        with st.expander("Add Class", expanded=False):
            with st.form("add_class_form"):
                subj = st.text_input("Course")
                day = st.selectbox("Day", DAYS)
                start = st.text_input("Start (HH:MM)", "09:00")
                end = st.text_input("End (HH:MM)", "10:00")
                if st.form_submit_button("Add"):
                    try:
                        new_id = add_class(tracker_id, subj, DAY_TO_INT[day], start, end)
                        st.session_state.undo_timetable = {"type": "add", "class_id": int(new_id)}
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

        with st.expander("Edit Class", expanded=False):
            if not classes:
                st.caption("No classes available.")
            else:
                day_e = st.selectbox("Day", DAYS, key="edit_day")
                day_classes = [c for c in classes if int(c["day_of_week"]) == DAY_TO_INT[day_e]]
                if not day_classes:
                    st.caption("No classes on this day.")
                else:
                    slots = sorted({f"{c['start_time']}–{c['end_time']}" for c in day_classes})
                    slot = st.selectbox("Time slot", slots, key="edit_slot")
                    stt, ent = slot.split("–")
                    slot_classes = [c for c in day_classes if c["start_time"] == stt and c["end_time"] == ent]
                    labels = {f"{c['subject']} (id:{c['class_id']})": c for c in slot_classes}
                    pick = st.selectbox("Class", list(labels.keys()), key="edit_pick")
                    target = labels[pick]

                    with st.form("edit_class_form"):
                        ns = st.text_input("Course", value=target["subject"])
                        nd = st.selectbox("Day", DAYS, index=int(target["day_of_week"]))
                        nst = st.text_input("Start (HH:MM)", value=target["start_time"])
                        net = st.text_input("End (HH:MM)", value=target["end_time"])
                        if st.form_submit_button("Save"):
                            try:
                                old = update_class(int(target["class_id"]), ns, DAY_TO_INT[nd], nst, net)
                                if old:
                                    st.session_state.undo_timetable = {"type": "edit", "class_id": int(target["class_id"]), "old": old}
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

        with st.expander("Delete Class", expanded=False):
            if not classes:
                st.caption("No classes available.")
            else:
                day_d = st.selectbox("Day", DAYS, key="del_day")
                day_classes = [c for c in classes if int(c["day_of_week"]) == DAY_TO_INT[day_d]]
                if not day_classes:
                    st.caption("No classes on this day.")
                else:
                    slots = sorted({f"{c['start_time']}–{c['end_time']}" for c in day_classes})
                    slot = st.selectbox("Time slot", slots, key="del_slot")
                    stt, ent = slot.split("–")
                    slot_classes = [c for c in day_classes if c["start_time"] == stt and c["end_time"] == ent]
                    labels = {f"{c['subject']} (id:{c['class_id']})": c for c in slot_classes}
                    pick = st.selectbox("Class", list(labels.keys()), key="del_pick")
                    target = labels[pick]

                    st.warning(f"Delete {target['subject']} on {day_d} {slot}?")
                    if st.button("Confirm Delete Class"):
                        old = delete_class(int(target["class_id"]))
                        if old:
                            st.session_state.undo_timetable = {"type": "delete", "old": old}
                        st.rerun()

        st.divider()
        st.header("Danger Zone")

        if st.button("Clear Timetable"):
            st.session_state.confirm_clear = True
            st.session_state.confirm_delete = False

        if st.session_state.confirm_clear:
            st.warning("This deletes all classes and all attendance sessions linked to them.")
            c1, c2 = st.columns(2)
            if c1.button("Confirm Clear"):
                clear_timetable(tracker_id)
                st.session_state.confirm_clear = False
                st.session_state.undo_timetable = None
                st.rerun()
            if c2.button("Cancel"):
                st.session_state.confirm_clear = False

        if st.button("Delete Tracker"):
            st.session_state.confirm_delete = True
            st.session_state.confirm_clear = False

        if st.session_state.confirm_delete:
            st.error("This deletes the tracker and all its data.")
            c1, c2 = st.columns(2)
            if c1.button("Confirm Delete"):
                delete_tracker(tracker_id)
                st.session_state.confirm_delete = False
                st.session_state.undo_timetable = None
                st.session_state.page = "home"
                st.session_state.active_tracker = None
                st.rerun()
            if c2.button("Cancel", key="cancel_delete_tracker"):
                st.session_state.confirm_delete = False


# -------------------- Pages --------------------
def reset_view_state():
    st.session_state.tracker_view = "summary"
    st.session_state.week_offset = 0
    st.session_state.undo_timetable = None
    st.session_state.confirm_clear = False
    st.session_state.confirm_delete = False
    st.session_state.last_attendance_action = None


def auth_page():
    st.title("Login")

    tabs = st.tabs(["Log In", "Sign Up"])

    with tabs[0]:
        with st.form("login_form"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Log In", type="primary"):
                uid = user_authenticate(u, p)
                if uid is None:
                    st.error("Invalid username or password.")
                else:
                    st.session_state.user_id = uid
                    st.session_state.page = "home"
                    reset_view_state()
                    st.rerun()

    with tabs[1]:
        with st.form("signup_form"):
            u = st.text_input("Username (case-insensitive)")
            p = st.text_input("Password", type="password")
            p2 = st.text_input("Confirm Password", type="password")
            if st.form_submit_button("Sign Up", type="primary"):
                if p != p2:
                    st.error("Passwords do not match.")
                else:
                    try:
                        uid = user_create(u, p)
                        st.success("Account created. You can log in now.")
                    except sqlite3.IntegrityError:
                        st.error("That username is already taken (case-insensitive).")
                    except Exception as e:
                        st.error(str(e))


def home_page(user_id: int):
    # Ensure user clone exists (and therefore user always sees a copy of global timetable)
    _ = get_or_create_user_clone(user_id)

    st.title("Trackers")

    # Conventional sidebar
    with st.sidebar:
        st.header("Account")
        st.write(get_user_display(user_id))
        if st.button("Log Out"):
            st.session_state.user_id = None
            st.session_state.page = "auth"
            reset_view_state()
            st.rerun()

    st.markdown("<div class='fab'><a href='?create=1'>+</a></div>", unsafe_allow_html=True)

    qp = {}
    try:
        qp = dict(st.query_params)
    except Exception:
        qp = st.experimental_get_query_params()
    show_create = False
    if "create" in qp:
        v = qp["create"]
        if isinstance(v, list):
            show_create = (v[0] == "1")
        else:
            show_create = (str(v) == "1")

    if show_create:
        st.subheader("Create Tracker")
        with st.form("create_tracker_form"):
            name = st.text_input("Name", "New Tracker")
            sd = st.date_input("Start date", date.today())
            ed = st.date_input("End date", date.today() + timedelta(days=120))
            c1, c2 = st.columns(2)
            if c1.form_submit_button("Create", type="primary"):
                try:
                    create_tracker_for_user(user_id, name, sd, ed)
                    try:
                        st.query_params.clear()
                    except Exception:
                        st.experimental_set_query_params()
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
            if c2.form_submit_button("Cancel"):
                try:
                    st.query_params.clear()
                except Exception:
                    st.experimental_set_query_params()
                st.rerun()
        st.markdown("---")

    ts = list_user_trackers(user_id)
    if not ts:
        st.caption("No trackers yet.")
        return

    cols_per_row = 3
    idx = 0
    while idx < len(ts):
        cols = st.columns(cols_per_row)
        for j in range(cols_per_row):
            if idx >= len(ts):
                break
            t = ts[idx]
            idx += 1
            tid = int(t["tracker_id"])
            bg = stable_color(str(tid), TRACKER_PALETTE)

            with cols[j]:
                st.markdown(
                    f"""
                    <div class='tracker-blob' style='background:{bg}'>
                      <h4>{t['name']}</h4>
                      <div class='tracker-meta'>{t['start_date']} → {t['end_date']}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                if st.button("Open", key=f"open_{tid}"):
                    st.session_state.page = "tracker"
                    st.session_state.active_tracker = tid
                    reset_view_state()
                    st.rerun()


def tracker_page(user_id: int):
    tid = int(st.session_state.active_tracker)
    t = get_tracker_for_user(user_id, tid)
    if not t:
        st.error("Tracker not found.")
        st.session_state.page = "home"
        st.session_state.active_tracker = None
        st.rerun()
        return

    tracker_start = date.fromisoformat(t["start_date"])
    tracker_end = date.fromisoformat(t["end_date"])

    top = st.columns([6, 2])
    with top[0]:
        st.title(t["name"])
        st.caption(f"{t['start_date']} → {t['end_date']}")
    with top[1]:
        if st.button("Back to Trackers"):
            st.session_state.page = "home"
            st.session_state.active_tracker = None
            reset_view_state()
            st.rerun()

    # toggle summary/tasks
    if "tracker_view" not in st.session_state:
        st.session_state.tracker_view = "summary"

    nav = st.columns([6, 2])
    with nav[1]:
        if st.session_state.tracker_view == "summary":
            if st.button("View Tasks"):
                st.session_state.tracker_view = "tasks"
                st.rerun()
        else:
            if st.button("View Summary"):
                st.session_state.tracker_view = "summary"
                st.rerun()

    if st.session_state.tracker_view == "summary":
        st.subheader("Course Summary")
        render_course_dashboard(user_id, tid)
        return

    # Tasks view:
    sidebar_editor(tid)

    # show prompts first (backlog)
    render_attendance_prompts(user_id, tid, tracker_start, tracker_end)
    render_modify_past_attendance(user_id, tid)

    st.markdown("---")
    render_week_view(user_id, tid, tracker_start, tracker_end)


# -------------------- App --------------------
def main():
    st.set_page_config(page_title="Attendance Trackers", layout="wide")
    inject_css()
    init_db()

    if "page" not in st.session_state:
        st.session_state.page = "auth"
    if "user_id" not in st.session_state:
        st.session_state.user_id = None
    if "active_tracker" not in st.session_state:
        st.session_state.active_tracker = None

    # Route
    if st.session_state.user_id is None:
        st.session_state.page = "auth"
        auth_page()
        return

    user_id = int(st.session_state.user_id)

    if st.session_state.page == "home":
        home_page(user_id)
    elif st.session_state.page == "tracker":
        if st.session_state.active_tracker is None:
            st.session_state.page = "home"
            home_page(user_id)
        else:
            tracker_page(user_id)
    else:
        # default
        st.session_state.page = "home"
        home_page(user_id)


if __name__ == "__main__":
    main()

