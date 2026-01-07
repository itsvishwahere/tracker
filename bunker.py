# attendance_app.py
# Streamlit 1.52.2 compatible
# Single DB: attendance.db
# Stable version + FIX: end-aligned compressed week layout with duration-based blob height

import sqlite3
from datetime import datetime, date, timedelta
from typing import Dict, List, Tuple, Optional

import streamlit as st

DB = "attendance.db"
POST_CLASS_BUFFER_MIN = 5  # prompt after end time + buffer

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_TO_INT = {d: i for i, d in enumerate(DAYS)}


# -------------------- DB --------------------
def conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.executescript(
            """
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

            CREATE TABLE IF NOT EXISTS sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id INTEGER NOT NULL,
                session_date TEXT NOT NULL,
                status TEXT CHECK(status IN ('PENDING','ATTENDED','MISSED','CANCELLED')) NOT NULL,
                UNIQUE(class_id, session_date)
            );
            """
        )

        if not c.execute("SELECT 1 FROM trackers LIMIT 1").fetchone():
            c.execute(
                "INSERT INTO trackers(name, created_at, start_date, end_date) VALUES (?,?,?,?)",
                (
                    "Default Tracker",
                    datetime.now().isoformat(timespec="seconds"),
                    date.today().isoformat(),
                    (date.today() + timedelta(days=120)).isoformat(),
                ),
            )


# -------------------- Helpers --------------------
def parse_time_to_minutes(hhmm: str) -> int:
    t = datetime.strptime(hhmm.strip(), "%H:%M").time()
    return t.hour * 60 + t.minute


def normalize_time(hhmm: str) -> str:
    """
    Normalizes '9:20' -> '09:20' to prevent slot-key mismatch and layout drift.
    """
    t = datetime.strptime(hhmm.strip(), "%H:%M").time()
    return f"{t.hour:02d}:{t.minute:02d}"


def monday_of_week(offset: int) -> date:
    today = date.today() + timedelta(days=offset * 7)
    return today - timedelta(days=today.weekday())


def earliest_monday_for_range(d: date) -> date:
    return d - timedelta(days=d.weekday())


def latest_monday_for_range(d: date) -> date:
    return d - timedelta(days=d.weekday())


def clamp_week_offset(week_offset: int, tracker_start: date, tracker_end: date) -> int:
    base = monday_of_week(0)
    target = monday_of_week(week_offset)

    earliest = earliest_monday_for_range(tracker_start)
    latest = latest_monday_for_range(tracker_end)

    if target < earliest:
        return (earliest - base).days // 7
    if target > latest:
        return (latest - base).days // 7
    return week_offset


# -------------------- Colors --------------------
TRACKER_PALETTE = [
    "#1abc9c", "#3498db", "#9b59b6", "#e67e22", "#e84393",
    "#16a085", "#2980b9", "#8e44ad", "#c0392b", "#2c3e50",
]
COURSE_PALETTE = [
    "#2ecc71", "#e74c3c", "#f1c40f", "#9b59b6", "#3498db",
    "#1abc9c", "#e67e22", "#34495e", "#d35400", "#8e44ad",
]


def stable_color(seed: str, palette: List[str]) -> str:
    h = 0
    for ch in seed:
        h = (h * 131 + ord(ch)) % 10_000_000
    return palette[h % len(palette)]


# -------------------- CSS --------------------
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

          .time-axis {
            font-size: 0.82rem;
            opacity: 0.70;
            padding-top: 8px;
            white-space: nowrap;
          }

          .day-box {
            border: 1px solid rgba(255,255,255,0.10);
            border-radius: 14px;
            padding: 10px;
            min-height: 60px;
            background: rgba(255,255,255,0.02);
          }

          .band-cell {
            display: flex;
            flex-direction: column;
            justify-content: flex-end;
            gap: 10px;
          }

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
          .pill-meta {
            font-weight: 650;
            font-size: 0.82rem;
            opacity: 0.95;
            margin-top: 4px;
          }
          .pill-status {
            margin-top: 10px;
            font-size: 0.76rem;
            padding: 1px 10px;
            border-radius: 999px;
            background: rgba(0,0,0,0.18);
            display: inline-block;
            width: fit-content;
          }

          .range-note { opacity: 0.75; font-size: 0.90rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# -------------------- Query param helpers (FAB) --------------------
def get_query_params() -> Dict[str, List[str]]:
    try:
        qp = dict(st.query_params)
        out: Dict[str, List[str]] = {}
        for k, v in qp.items():
            if isinstance(v, list):
                out[k] = v
            else:
                out[k] = [str(v)]
        return out
    except Exception:
        return st.experimental_get_query_params()


def set_query_params(**kwargs):
    try:
        st.query_params.clear()
        for k, v in kwargs.items():
            st.query_params[k] = v
    except Exception:
        st.experimental_set_query_params(**kwargs)


# -------------------- Data access --------------------
def trackers():
    with conn() as c:
        return c.execute("SELECT * FROM trackers ORDER BY tracker_id").fetchall()


def get_tracker(tracker_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM trackers WHERE tracker_id=?", (tracker_id,)).fetchone()


def create_tracker(name: str, start_date: date, end_date: date):
    name = (name or "").strip() or "Untitled Tracker"
    if end_date < start_date:
        raise ValueError("End date must be on/after start date.")
    with conn() as c:
        c.execute(
            "INSERT INTO trackers(name, created_at, start_date, end_date) VALUES (?,?,?,?)",
            (name, datetime.now().isoformat(timespec="seconds"), start_date.isoformat(), end_date.isoformat()),
        )


def list_classes(tracker_id: int):
    with conn() as c:
        return c.execute(
            "SELECT * FROM classes WHERE tracker_id=? ORDER BY day_of_week, start_time, end_time, subject",
            (tracker_id,),
        ).fetchall()


def add_class(tracker_id: int, subject: str, day: int, start: str, end: str) -> int:
    subject = (subject or "").strip()
    if not subject:
        raise ValueError("Course cannot be empty.")

    start_n = normalize_time(start)
    end_n = normalize_time(end)

    if parse_time_to_minutes(end_n) <= parse_time_to_minutes(start_n):
        raise ValueError("End time must be after start time.")

    with conn() as c:
        c.execute(
            "INSERT INTO classes(subject, day_of_week, start_time, end_time, tracker_id) VALUES (?,?,?,?,?)",
            (subject, day, start_n, end_n, tracker_id),
        )
        return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()[0])


def update_class(class_id: int, subject: str, day: int, start: str, end: str) -> Optional[Dict]:
    subject = (subject or "").strip()
    if not subject:
        raise ValueError("Course cannot be empty.")

    start_n = normalize_time(start)
    end_n = normalize_time(end)

    if parse_time_to_minutes(end_n) <= parse_time_to_minutes(start_n):
        raise ValueError("End time must be after start time.")

    today_iso = date.today().isoformat()

    with conn() as c:
        old = c.execute(
            "SELECT class_id, subject, day_of_week, start_time, end_time, tracker_id FROM classes WHERE class_id=?",
            (class_id,),
        ).fetchone()

        c.execute(
            "UPDATE classes SET subject=?, day_of_week=?, start_time=?, end_time=? WHERE class_id=?",
            (subject, day, start_n, end_n, class_id),
        )

        # prevent "duplicate future schedule" bug
        c.execute(
            "DELETE FROM sessions WHERE class_id=? AND status='PENDING' AND session_date>=?",
            (class_id, today_iso),
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


def ensure_sessions_for_week(tracker_id: int, week_start: date, tracker_start: date, tracker_end: date):
    classes = list_classes(tracker_id)
    with conn() as c:
        for cl in classes:
            d = week_start + timedelta(days=int(cl["day_of_week"]))
            if d < tracker_start or d > tracker_end:
                continue
            c.execute(
                "INSERT OR IGNORE INTO sessions(class_id, session_date, status) VALUES (?,?, 'PENDING')",
                (cl["class_id"], d.isoformat()),
            )


def get_sessions_for_week(tracker_id: int, week_start: date):
    dates = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]
    with conn() as c:
        return c.execute(
            f"""
            SELECT s.*, c.subject, c.start_time, c.end_time
            FROM sessions s
            JOIN classes c ON c.class_id=s.class_id
            WHERE c.tracker_id=?
              AND s.session_date IN ({','.join('?'*7)})
            ORDER BY s.session_date, c.start_time, c.end_time, c.subject
            """,
            (tracker_id, *dates),
        ).fetchall()


def set_status(session_id: int, status: str):
    with conn() as c:
        c.execute("UPDATE sessions SET status=? WHERE session_id=?", (status, session_id))


def course_stats(tracker_id: int) -> List[Dict]:
    with conn() as c:
        rows = c.execute(
            """
            SELECT c.subject,
                   SUM(s.status='ATTENDED') AS a,
                   SUM(s.status='MISSED')   AS m,
                   SUM(s.status='CANCELLED') AS cx
            FROM sessions s
            JOIN classes c ON c.class_id=s.class_id
            WHERE c.tracker_id=?
            GROUP BY c.subject
            ORDER BY c.subject
            """,
            (tracker_id,),
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


def apply_undo(action: Dict):
    today_iso = date.today().isoformat()

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
            c.execute(
                "DELETE FROM sessions WHERE class_id=? AND status='PENDING' AND session_date>=?",
                (cid, today_iso),
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

    raise ValueError(f"Unsupported undo type: {typ}")


# -------------------- UI / Pages --------------------
def reset_tracker_view_state():
    st.session_state.tracker_view = "summary"
    st.session_state.week_offset = 0
    st.session_state.undo_action = None
    st.session_state.confirm_clear_flag = False
    st.session_state.confirm_delete_flag = False


def home():
    st.title("Attendance Trackers")

    st.markdown("<div class='fab'><a href='?create=1'>+</a></div>", unsafe_allow_html=True)
    qp = get_query_params()
    show_create = (qp.get("create", [""])[0] == "1")

    if show_create:
        st.subheader("Create new tracker")
        st.info("Overlapping classes are allowed in this tracker.")
        with st.form("create_tracker_form"):
            name = st.text_input("Name", "New Tracker")
            sd = st.date_input("Start date", date.today())
            ed = st.date_input("End date", date.today() + timedelta(days=120))
            c1, c2 = st.columns(2)
            if c1.form_submit_button("Create", type="primary"):
                try:
                    create_tracker(name, sd, ed)
                    set_query_params()
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
            if c2.form_submit_button("Cancel"):
                set_query_params()
                st.rerun()

        st.markdown("---")

    ts = trackers()
    if not ts:
        st.info("No trackers yet. Use the + button to create one.")
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
                    reset_tracker_view_state()
                    st.rerun()


def render_course_dashboard(tracker_id: int):
    stats = course_stats(tracker_id)
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
                    st.plotly_chart(fig, use_container_width=True, key=f"gauge_{tracker_id}_{course}")
                else:
                    st.metric("Attendance", f"{pct:.2f}%")

                st.caption(f"A:{item['Attended']} · M:{item['Missed']} · C:{item['Cancelled']}")


def _duration_to_height_px(duration_min: int) -> int:
    """
    Blob height is proportional to duration. Keep it compact and readable.
    """
    # baseline and scaling chosen to look right in Streamlit layout
    base = 64
    px_per_min = 1.2
    h = int(base + duration_min * px_per_min)
    return max(70, min(h, 190))


def render_week_view(tracker_id: int, tracker_start: date, tracker_end: date):
    """
    FIXED alignment model:
    - Rows (bands) are grouped by END TIME (end-aligned).
    - Inside a band, blobs are bottom-aligned; shorter duration => shorter blob height.
    - Adds a dedicated time-axis column to eliminate "floating timestamp nonsense".
    - Still compressed: no empty hour-grid.
    """
    st.session_state.week_offset = clamp_week_offset(int(st.session_state.week_offset), tracker_start, tracker_end)
    week_start = monday_of_week(int(st.session_state.week_offset))

    earliest = earliest_monday_for_range(tracker_start)
    latest = latest_monday_for_range(tracker_end)

    can_go_prev = (week_start - timedelta(days=7)) >= earliest
    can_go_next = (week_start + timedelta(days=7)) <= latest

    nav = st.columns([1, 6, 1])
    if nav[0].button("◀", disabled=not can_go_prev, key="btn_prev_week"):
        st.session_state.week_offset -= 1
        st.rerun()
    if nav[2].button("▶", disabled=not can_go_next, key="btn_next_week"):
        st.session_state.week_offset += 1
        st.rerun()

    week_start = monday_of_week(int(st.session_state.week_offset))

    ensure_sessions_for_week(tracker_id, week_start, tracker_start, tracker_end)
    sessions = get_sessions_for_week(tracker_id, week_start)

    st.subheader("Week view")
    st.markdown(
        f"<div class='range-note'>Visible range: <b>{tracker_start.isoformat()}</b> → <b>{tracker_end.isoformat()}</b></div>",
        unsafe_allow_html=True,
    )

    # Normalize times for safe grouping
    normalized_sessions = []
    for s in sessions:
        stt = normalize_time(s["start_time"])
        ent = normalize_time(s["end_time"])
        session_date = datetime.fromisoformat(s["session_date"]).date()
        weekday = session_date.weekday()
        start_min = parse_time_to_minutes(stt)
        end_min = parse_time_to_minutes(ent)
        normalized_sessions.append(
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

    # Group by end time (end-aligned bands)
    bands: Dict[int, Dict] = {}
    for it in normalized_sessions:
        end_min = it["end_min"]
        bands.setdefault(end_min, {"end_min": end_min, "end_str": it["end_str"], "items": []})
        bands[end_min]["items"].append(it)

    # Sort bands by end time ascending
    band_list = sorted(bands.values(), key=lambda b: b["end_min"])

    # Header row: add time axis + 7 days
    header_cols = st.columns([1.2] + [1] * 7)
    header_cols[0].markdown(" ")
    for i in range(7):
        day_date = week_start + timedelta(days=i)
        if day_date < tracker_start or day_date > tracker_end:
            header_cols[i + 1].markdown(" ")
            continue
        header_cols[i + 1].markdown(
            f"<div class='dayhead'>{DAYS[i]} · {day_date.strftime('%d %b')}</div>",
            unsafe_allow_html=True,
        )

    # Render each band row
    for band in band_list:
        end_str = band["end_str"]
        items = band["items"]

        # Compute band height = max event height within the band (for bottom-alignment container)
        max_h = 0
        for it in items:
            max_h = max(max_h, _duration_to_height_px(it["duration"]))

        row_cols = st.columns([1.2] + [1] * 7)
        # time axis label (end time)
        row_cols[0].markdown(f"<div class='time-axis'>… → {end_str}</div>", unsafe_allow_html=True)

        # For each day column, bottom-align within same band height
        for day_idx in range(7):
            day_date = week_start + timedelta(days=day_idx)
            if day_date < tracker_start or day_date > tracker_end:
                row_cols[day_idx + 1].markdown(" ")
                continue

            day_items = [it for it in items if it["weekday"] == day_idx]
            if not day_items:
                # empty but keep day-box for consistent structure
                row_cols[day_idx + 1].markdown(f"<div class='day-box' style='height:{max_h}px'></div>", unsafe_allow_html=True)
                continue

            # sort by start time within the band (stable)
            day_items.sort(key=lambda it: (it["start_min"], it["duration"], it["row"]["subject"]))

            # build HTML block with bottom alignment
            blocks = []
            for it in day_items:
                s = it["row"]
                course = s["subject"]
                color = stable_color(course, COURSE_PALETTE)
                h = _duration_to_height_px(it["duration"])
                stt = it["start_str"]
                ent = it["end_str"]
                blocks.append(
                    f"""
                    <div class='course-pill' style='background:{color}; height:{h}px'>
                      <div>{course}</div>
                      <div class='pill-meta'>{stt}–{ent}</div>
                      <div class='pill-status'>{s['status']}</div>
                    </div>
                    """
                )

            html = (
                f"<div class='day-box band-cell' style='height:{max_h}px'>"
                + "".join(blocks)
                + "</div>"
            )
            row_cols[day_idx + 1].markdown(html, unsafe_allow_html=True)

    return sessions


def render_prompts_for_today(week_sessions: List[sqlite3.Row]):
    st.subheader("Today — attendance prompts")

    now = datetime.now()
    today_iso = date.today().isoformat()
    prompted = False

    for s in week_sessions:
        if s["session_date"] != today_iso or s["status"] != "PENDING":
            continue

        end_dt = datetime.combine(date.today(), datetime.strptime(s["end_time"], "%H:%M").time()) + timedelta(
            minutes=POST_CLASS_BUFFER_MIN
        )
        if now < end_dt:
            continue

        prompted = True
        st.write(f"**{s['subject']}** ({s['start_time']}–{s['end_time']})")
        a, b, c = st.columns(3)
        if a.button("✅ Attended", key=f"att_{s['session_id']}"):
            set_status(int(s["session_id"]), "ATTENDED")
            st.rerun()
        if b.button("🚫 Cancelled", key=f"can_{s['session_id']}"):
            set_status(int(s["session_id"]), "CANCELLED")
            st.rerun()
        if c.button("❌ Missed", key=f"mis_{s['session_id']}"):
            set_status(int(s["session_id"]), "MISSED")
            st.rerun()
        st.markdown("---")

    if not prompted:
        st.caption("No pending prompts right now (shown after end time + buffer).")


def sidebar_editor(tracker_id: int):
    classes = list_classes(tracker_id)

    with st.sidebar:
        st.header("Edit")

        st.subheader("Undo")
        if st.button("Undo last timetable change", key="undo_btn", disabled=not bool(st.session_state.undo_action)):
            try:
                apply_undo(st.session_state.undo_action)
                st.session_state.undo_action = None
                st.rerun()
            except Exception as e:
                st.error(f"Undo failed: {e}")

        with st.expander("➕ Add class", expanded=False):
            with st.form("add_class_form"):
                subj = st.text_input("Course", key="add_course")
                day = st.selectbox("Day", DAYS, key="add_day")
                start = st.text_input("Start (HH:MM)", "09:00", key="add_start")
                end = st.text_input("End (HH:MM)", "10:00", key="add_end")
                if st.form_submit_button("Add", type="primary"):
                    try:
                        new_id = add_class(tracker_id, subj, DAY_TO_INT[day], start, end)
                        st.session_state.undo_action = {"type": "add", "class_id": int(new_id)}
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

        with st.expander("✏️ Edit class", expanded=False):
            if not classes:
                st.info("No classes to edit.")
            else:
                day_e = st.selectbox("Day", DAYS, key="edit_day")
                day_classes = [c for c in classes if int(c["day_of_week"]) == DAY_TO_INT[day_e]]
                if not day_classes:
                    st.info("No classes on that day.")
                else:
                    slots = sorted({f"{c['start_time']}–{c['end_time']}" for c in day_classes})
                    slot = st.selectbox("Time slot", slots, key="edit_slot")
                    stt, ent = slot.split("–")
                    slot_classes = [c for c in day_classes if c["start_time"] == stt and c["end_time"] == ent]
                    labels = {f"{c['subject']} (id:{c['class_id']})": c for c in slot_classes}
                    pick = st.selectbox("Course", list(labels.keys()), key="edit_course_pick")
                    target = labels[pick]

                    with st.form("edit_class_form"):
                        ns = st.text_input("Course", value=target["subject"], key="edit_course_new")
                        nd = st.selectbox("Day (new)", DAYS, index=int(target["day_of_week"]), key="edit_day_new")
                        nst = st.text_input("Start (HH:MM)", value=target["start_time"], key="edit_start_new")
                        net = st.text_input("End (HH:MM)", value=target["end_time"], key="edit_end_new")
                        if st.form_submit_button("Save", type="primary"):
                            try:
                                old = update_class(int(target["class_id"]), ns, DAY_TO_INT[nd], nst, net)
                                if old:
                                    st.session_state.undo_action = {"type": "edit", "class_id": int(target["class_id"]), "old": old}
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

        with st.expander("🗑️ Delete class", expanded=False):
            if not classes:
                st.info("No classes to delete.")
            else:
                day_d = st.selectbox("Day", DAYS, key="del_day")
                day_classes = [c for c in classes if int(c["day_of_week"]) == DAY_TO_INT[day_d]]
                if not day_classes:
                    st.info("No classes on that day.")
                else:
                    slots = sorted({f"{c['start_time']}–{c['end_time']}" for c in day_classes})
                    slot = st.selectbox("Time slot", slots, key="del_slot")
                    stt, ent = slot.split("–")
                    slot_classes = [c for c in day_classes if c["start_time"] == stt and c["end_time"] == ent]
                    labels = {f"{c['subject']} (id:{c['class_id']})": c for c in slot_classes}
                    pick = st.selectbox("Course", list(labels.keys()), key="del_course_pick")
                    target = labels[pick]

                    st.warning(f"You are about to delete: {target['subject']} on {day_d} {slot}.")
                    if st.button("Confirm delete", key="confirm_delete_class", type="primary"):
                        old = delete_class(int(target["class_id"]))
                        if old:
                            st.session_state.undo_action = {"type": "delete", "old": old}
                        st.rerun()

        st.divider()
        st.header("Danger Zone")

        if st.button("Clear Timetable", key="btn_clear_tt"):
            st.session_state.confirm_clear_flag = True
            st.session_state.confirm_delete_flag = False

        if st.session_state.confirm_clear_flag:
            st.warning("This will delete ALL classes and ALL attendance sessions for this tracker. The tracker remains.")
            c1, c2 = st.columns(2)
            if c1.button("Confirm Clear", key="btn_confirm_clear", type="primary"):
                clear_timetable(tracker_id)
                st.session_state.confirm_clear_flag = False
                st.session_state.undo_action = None
                st.rerun()
            if c2.button("Cancel", key="btn_cancel_clear"):
                st.session_state.confirm_clear_flag = False

        if st.button("Delete Tracker", key="btn_delete_tracker"):
            st.session_state.confirm_delete_flag = True
            st.session_state.confirm_clear_flag = False

        if st.session_state.confirm_delete_flag:
            st.error("This will permanently delete the tracker and all its data. This cannot be undone.")
            c1, c2 = st.columns(2)
            if c1.button("Confirm Delete", key="btn_confirm_delete", type="primary"):
                delete_tracker(tracker_id)
                st.session_state.confirm_delete_flag = False
                st.session_state.undo_action = None
                st.session_state.page = "home"
                st.session_state.active_tracker = None
                reset_tracker_view_state()
                st.rerun()
            if c2.button("Cancel", key="btn_cancel_delete"):
                st.session_state.confirm_delete_flag = False


def tracker_page():
    tid = st.session_state.active_tracker
    t = get_tracker(int(tid))
    if not t:
        st.error("Tracker not found.")
        st.session_state.page = "home"
        st.session_state.active_tracker = None
        reset_tracker_view_state()
        st.rerun()
        return

    tracker_start = date.fromisoformat(t["start_date"])
    tracker_end = date.fromisoformat(t["end_date"])

    top = st.columns([6, 2])
    with top[0]:
        st.title(t["name"])
        st.caption(f"{t['start_date']} → {t['end_date']}")
    with top[1]:
        if st.button("← Back to Landing Page", key="btn_back_home"):
            st.session_state.page = "home"
            st.session_state.active_tracker = None
            reset_tracker_view_state()
            st.rerun()

    nav = st.columns([6, 2])
    with nav[1]:
        if st.session_state.tracker_view == "summary":
            if st.button("View Upcoming Tasks →", key="btn_to_tasks"):
                st.session_state.tracker_view = "tasks"
                st.rerun()
        else:
            if st.button("← Back to Summary", key="btn_to_summary"):
                st.session_state.tracker_view = "summary"
                st.rerun()

    if st.session_state.tracker_view == "summary":
        st.subheader("Course-wise attendance")
        render_course_dashboard(int(tid))
        return

    sidebar_editor(int(tid))
    week_sessions = render_week_view(int(tid), tracker_start, tracker_end)
    render_prompts_for_today(week_sessions)


# -------------------- App --------------------
def main():
    st.set_page_config(page_title="Attendance Trackers", layout="wide")
    inject_css()
    init_db()

    if "page" not in st.session_state:
        st.session_state.page = "home"
    if "active_tracker" not in st.session_state:
        st.session_state.active_tracker = None
    if "tracker_view" not in st.session_state:
        st.session_state.tracker_view = "summary"
    if "week_offset" not in st.session_state:
        st.session_state.week_offset = 0
    if "undo_action" not in st.session_state:
        st.session_state.undo_action = None
    if "confirm_clear_flag" not in st.session_state:
        st.session_state.confirm_clear_flag = False
    if "confirm_delete_flag" not in st.session_state:
        st.session_state.confirm_delete_flag = False

    if st.session_state.page == "home":
        home()
    else:
        if st.session_state.active_tracker is None:
            st.session_state.page = "home"
            reset_tracker_view_state()
            st.rerun()
        tracker_page()


if __name__ == "__main__":
    main()
