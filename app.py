from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, send_file
import sqlite3
import random
import re
import secrets
import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production-12345'

BASE_URL = "https://swordlike-rosella-nonmelodramatic.ngrok-free.dev"


# =========================================================
# DATABASE
# =========================================================
def get_db():
    conn = sqlite3.connect("timetable.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _col_names(cur, table):
    """Return set of column names for a table (empty set if table does not exist)."""
    try:
        cur.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cur.fetchall()}
    except Exception:
        return set()


def _table_exists(cur, table):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def migrate_db():
    conn = sqlite3.connect("timetable.db", timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        cur = conn.cursor()

        if _table_exists(cur, "allocation_rounds"):
            ar_cols = _col_names(cur, "allocation_rounds")
            cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='allocation_rounds'")
            row = cur.fetchone()
            create_sql = row[0] if row else ""
            needs_rebuild = False
            if "semester" in ar_cols and "semester_type" not in ar_cols:
                needs_rebuild = True
            elif "semester_type" not in ar_cols:
                needs_rebuild = True
            elif "semester" in ar_cols and "semester_type" in ar_cols:
                needs_rebuild = True
            elif "semester" in create_sql.lower() and "NOT NULL" in create_sql and "semester_type" in ar_cols:
                needs_rebuild = True

            if needs_rebuild:
                cur.execute("ALTER TABLE allocation_rounds RENAME TO _ar_backup_migration")
                cur.execute("""
                    CREATE TABLE allocation_rounds(
                        round_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                        dept_id       INTEGER NOT NULL,
                        semester_type TEXT    NOT NULL DEFAULT 'odd',
                        status        TEXT    NOT NULL DEFAULT 'active',
                        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )""")
                backup_cols = _col_names(cur, "_ar_backup_migration")
                if "semester_type" in backup_cols:
                    cur.execute("""
                        INSERT INTO allocation_rounds
                               (round_id, dept_id, semester_type, status, created_at)
                        SELECT  round_id, dept_id, semester_type, status, created_at
                        FROM _ar_backup_migration
                    """)
                elif "semester" in backup_cols:
                    cur.execute("""
                        INSERT INTO allocation_rounds
                               (round_id, dept_id, semester_type, status, created_at)
                        SELECT  round_id, dept_id,
                                CASE WHEN (CAST(semester AS INTEGER) % 2) = 1
                                     THEN 'odd' ELSE 'even' END,
                                status, created_at
                        FROM _ar_backup_migration
                    """)
                cur.execute("DROP TABLE _ar_backup_migration")
                conn.commit()

        if _table_exists(cur, "allocation_tokens"):
            at_cols = _col_names(cur, "allocation_tokens")
            for col, defn in [
                ("theory_quota", "INTEGER NOT NULL DEFAULT 2"),
                ("lab_quota",    "INTEGER NOT NULL DEFAULT 2"),
                ("submitted_at", "TIMESTAMP"),
            ]:
                if col not in at_cols:
                    try:
                        cur.execute(f"ALTER TABLE allocation_tokens ADD COLUMN {col} {defn}")
                    except Exception:
                        pass
            conn.commit()

    finally:
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        conn.close()


def init_db():
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS departments(
            dept_id   INTEGER PRIMARY KEY,
            dept_name TEXT NOT NULL
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS faculties(
            faculty_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_name   TEXT NOT NULL,
            dept_id        INTEGER,
            faculty_type   TEXT NOT NULL DEFAULT 'regular',
            visiting_days  TEXT DEFAULT '',
            seniority_rank INTEGER DEFAULT 9999,
            faculty_email  TEXT DEFAULT '',
            FOREIGN KEY(dept_id) REFERENCES departments(dept_id)
        )""")

        for col, default in [
            ("faculty_type",   "'regular'"),
            ("visiting_days",  "''"),
            ("seniority_rank", "9999"),
            ("faculty_email",  "''"),
        ]:
            try:
                cur.execute(f"ALTER TABLE faculties ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception:
                pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS courses(
            course_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            course_name       TEXT,
            course_code       TEXT,
            semester          INTEGER,
            credits           INTEGER,
            faculty_id        INTEGER,
            dept_id           INTEGER,
            course_type       TEXT NOT NULL DEFAULT 'theory',
            elective_group_id INTEGER DEFAULT NULL,
            FOREIGN KEY(faculty_id) REFERENCES faculties(faculty_id),
            FOREIGN KEY(dept_id)    REFERENCES departments(dept_id)
        )""")

        try:
            cur.execute("ALTER TABLE courses ADD COLUMN elective_group_id INTEGER DEFAULT NULL")
        except Exception:
            pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS elective_groups(
            group_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT NOT NULL,
            dept_id    INTEGER,
            semester   INTEGER,
            is_lab     INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(dept_id) REFERENCES departments(dept_id)
        )""")

        try:
            cur.execute("ALTER TABLE elective_groups ADD COLUMN is_lab INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS elective_options(
            option_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id      INTEGER NOT NULL,
            option_name   TEXT NOT NULL,
            option_code   TEXT DEFAULT '',
            option_credits INTEGER DEFAULT 3,
            FOREIGN KEY(group_id) REFERENCES elective_groups(group_id)
        )""")

        for col, default in [("option_code", "''"), ("option_credits", "3")]:
            try:
                cur.execute(f"ALTER TABLE elective_options ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception:
                pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS timetable_settings(
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            periods_per_day   INTEGER,
            period_duration   INTEGER,
            number_of_breaks  INTEGER,
            break_details     TEXT,
            working_days      TEXT DEFAULT 'Mon,Tue,Wed,Thu,Fri,Sat',
            start_time        TEXT DEFAULT '09:00'
        )""")

        for col, default in [
            ("working_days", "'Mon,Tue,Wed,Thu,Fri,Sat'"),
            ("start_time",   "'09:00'"),
        ]:
            try:
                cur.execute(f"ALTER TABLE timetable_settings ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception:
                pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS generated_timetable(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            dept_id    INTEGER,
            semester   INTEGER,
            day        TEXT,
            period     INTEGER,
            course_id  INTEGER,
            faculty_id INTEGER,
            FOREIGN KEY(dept_id)    REFERENCES departments(dept_id),
            FOREIGN KEY(course_id)  REFERENCES courses(course_id),
            FOREIGN KEY(faculty_id) REFERENCES faculties(faculty_id)
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS allocation_rounds(
            round_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            dept_id       INTEGER NOT NULL,
            semester_type TEXT    NOT NULL DEFAULT 'odd',
            status        TEXT    NOT NULL DEFAULT 'active',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(dept_id) REFERENCES departments(dept_id)
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS allocation_tokens(
            token_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id       INTEGER NOT NULL,
            faculty_id     INTEGER NOT NULL,
            seniority_rank INTEGER NOT NULL,
            token          TEXT UNIQUE NOT NULL,
            status         TEXT NOT NULL DEFAULT 'waiting',
            theory_quota   INTEGER NOT NULL DEFAULT 2,
            lab_quota      INTEGER NOT NULL DEFAULT 2,
            submitted_at   TIMESTAMP,
            FOREIGN KEY(round_id)   REFERENCES allocation_rounds(round_id),
            FOREIGN KEY(faculty_id) REFERENCES faculties(faculty_id)
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS faculty_allocations(
            alloc_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id   INTEGER NOT NULL,
            token_id   INTEGER NOT NULL,
            faculty_id INTEGER NOT NULL,
            course_id  INTEGER NOT NULL,
            role       TEXT NOT NULL DEFAULT 'primary',
            FOREIGN KEY(round_id)   REFERENCES allocation_rounds(round_id),
            FOREIGN KEY(faculty_id) REFERENCES faculties(faculty_id),
            FOREIGN KEY(course_id)  REFERENCES courses(course_id)
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS smtp_config(
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            smtp_email       TEXT NOT NULL,
            smtp_password    TEXT NOT NULL,
            smtp_sender_name TEXT NOT NULL DEFAULT 'TimeTable Pro',
            smtp_reply_to    TEXT DEFAULT '',
            smtp_configured  INTEGER NOT NULL DEFAULT 0
        )""")

        conn.commit()
    finally:
        conn.close()


migrate_db()
init_db()


# =========================================================
# HELPERS
# =========================================================
def compute_period_times(start_time_str, period_duration, periods_per_day, break_details_str):
    breaks = {}
    if break_details_str:
        try:
            for b in json.loads(break_details_str):
                breaks[int(b["after_period"])] = int(b["duration"])
        except Exception:
            pass
    try:
        h, m = map(int, start_time_str.split(":"))
        current = h * 60 + m
    except Exception:
        current = 9 * 60

    def fmt(mins):
        return f"{mins // 60:02d}:{mins % 60:02d}"

    period_times = {}
    break_times  = {}
    for p in range(1, periods_per_day + 1):
        period_end = current + period_duration
        period_times[p] = f"{fmt(current)} - {fmt(period_end)}"
        current = period_end
        if p in breaks:
            break_end = current + breaks[p]
            break_times[p] = f"{fmt(current)} - {fmt(break_end)}"
            current = break_end

    return period_times, break_times


def _quota_for_rank(rank, total):
    if total == 0:
        return 2, 2
    midpoint = total / 2
    if rank <= midpoint:
        return 2, 2
    return 1, 3


def get_smtp_config():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM smtp_config LIMIT 1")
        row = cur.fetchone()
        return row
    finally:
        conn.close()


def send_allocation_email(to_email, faculty_name, form_link, semester_type, dept_name, smtp_cfg):
    if not smtp_cfg or not smtp_cfg["smtp_configured"]:
        return False, "SMTP not configured"
    if not to_email:
        return False, "No email address for this faculty"

    sem_label = "Odd Semesters (1, 3, 5, 7)" if semester_type == "odd" else "Even Semesters (2, 4, 6, 8)"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Course Preference Form — {dept_name} {sem_label}"
        msg["From"]    = f"{smtp_cfg['smtp_sender_name']} <{smtp_cfg['smtp_email']}>"
        msg["To"]      = to_email
        if smtp_cfg["smtp_reply_to"]:
            msg["Reply-To"] = smtp_cfg["smtp_reply_to"]

        text_body = f"""Dear {faculty_name},

Your course preference form for {dept_name} — {sem_label} is now open.

Please click the link below to submit your preferences:
{form_link}

This link is unique to you. Please do not share it.
You must submit before the next faculty member's turn begins.

— {smtp_cfg['smtp_sender_name']}
"""

        html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; background: #f0f4ff; margin: 0; padding: 32px 16px; color: #1a1a2e; }}
  .card {{ background: white; border-radius: 16px; max-width: 520px; margin: 0 auto; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.10); }}
  .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #2d3561 100%); padding: 32px 36px 28px; text-align: center; }}
  .header h1 {{ color: #ecb365; font-size: 22px; margin: 0 0 4px; }}
  .header p  {{ color: rgba(255,255,255,0.55); font-size: 13px; margin: 0; }}
  .body {{ padding: 32px 36px; }}
  .body p {{ font-size: 15px; line-height: 1.7; color: #374151; margin-bottom: 16px; }}
  .btn {{ display: inline-block; background: linear-gradient(135deg, #c84b31, #e06040); color: white !important; text-decoration: none; padding: 14px 32px; border-radius: 10px; font-size: 15px; font-weight: 700; margin: 8px 0 24px; }}
  .link-box {{ background: #f3f4f8; border-radius: 8px; padding: 12px 16px; font-size: 13px; color: #6b7280; word-break: break-all; border: 1px solid #e5e7eb; margin-bottom: 20px; }}
  .warning {{ background: #fef3c7; border: 1px solid #fde68a; border-radius: 8px; padding: 12px 16px; font-size: 13px; color: #92400e; }}
  .footer {{ background: #f9faff; padding: 18px 36px; text-align: center; font-size: 12px; color: #9ca3af; border-top: 1px solid #f0f1f5; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <h1>TimeTable Pro</h1>
    <p>Course Preference Form</p>
  </div>
  <div class="body">
    <p>Dear <strong>{faculty_name}</strong>,</p>
    <p>Your course preference form for <strong>{dept_name} — {sem_label}</strong> is now open.</p>
    <div style="text-align:center">
      <a href="{form_link}" class="btn">Open My Preference Form</a>
    </div>
    <p style="font-size:13px;color:#6b7280;">Or copy this link into your browser:</p>
    <div class="link-box">{form_link}</div>
    <div class="warning">
      ⚠️ This link is <strong>unique to you</strong>. Do not share it.
    </div>
  </div>
  <div class="footer">Sent by {smtp_cfg['smtp_sender_name']} &bull; This is an automated message</div>
</div>
</body>
</html>"""

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_cfg["smtp_email"], smtp_cfg["smtp_password"])
            server.sendmail(smtp_cfg["smtp_email"], to_email, msg.as_string())

        return True, None

    except smtplib.SMTPAuthenticationError:
        return False, "Gmail authentication failed. Check your App Password."
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {str(e)}"
    except Exception as e:
        return False, f"Error: {str(e)}"


# =========================================================
# GLOBAL CONTEXT
# =========================================================
@app.context_processor
def inject_departments():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments")
        depts = cur.fetchall()
        return dict(nav_departments=depts)
    finally:
        conn.close()


# =========================================================
# HOME / ABOUT / CONTACT
# =========================================================
@app.route("/")
def home():
    return render_template("home.html")

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route("/choose-role")
def choose_role():
    return render_template("choose_role.html")


@app.route("/api/check-setup")
def api_check_setup():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM departments");         dept_count   = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM faculties");           fac_count    = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM courses");             course_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM generated_timetable"); tt_count     = cur.fetchone()[0]
        ready = dept_count > 0 and fac_count > 0 and course_count > 0 and tt_count > 0
        return jsonify({"ready": ready})
    finally:
        conn.close()


# =========================================================
# STUDENT TIMETABLE
# =========================================================
@app.route("/student", methods=["GET", "POST"])
def student():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments")
        departments = cur.fetchall()

        timetable       = None
        selected_dept   = None
        selected_sem    = None
        days            = []
        periods_per_day = 0
        period_times    = {}
        break_times     = {}

        if request.method == "POST":
            dept_id  = request.form.get("dept_id")
            semester = request.form.get("semester")
            selected_dept = dept_id
            selected_sem  = semester

            cur.execute("SELECT * FROM timetable_settings LIMIT 1")
            settings = cur.fetchone()
            if settings:
                periods_per_day = settings["periods_per_day"]
                working_days    = settings["working_days"] or "Mon,Tue,Wed,Thu,Fri,Sat"
                days            = [d.strip() for d in working_days.split(",")]
                start_time      = settings["start_time"] or "09:00"
                period_times, break_times = compute_period_times(
                    start_time, settings["period_duration"], periods_per_day, settings["break_details"])

            cur.execute("""
                SELECT gt.day, gt.period, gt.course_id,
                       c.course_name, c.course_code, c.course_type, c.elective_group_id,
                       c.faculty_id as primary_fid,
                       f.faculty_name, f.faculty_type, eg.group_name,
                       gt.faculty_id as slot_fid
                FROM generated_timetable gt
                JOIN courses c   ON gt.course_id  = c.course_id
                JOIN faculties f ON gt.faculty_id = f.faculty_id
                LEFT JOIN elective_groups eg ON c.elective_group_id = eg.group_id
                WHERE gt.dept_id=? AND gt.semester=?
                ORDER BY gt.day, gt.period, gt.course_id,
                         CASE WHEN gt.faculty_id = c.faculty_id THEN 0 ELSE 1 END
            """, (dept_id, semester))

            rows = cur.fetchall()
            seen_course_slot = set()
            timetable = {}
            for row in rows:
                key        = (row["day"], row["period"])
                course_key = (row["day"], row["period"], row["course_id"])
                if course_key in seen_course_slot:
                    continue
                seen_course_slot.add(course_key)
                entry = {
                    "course_name": row["course_name"], "course_code": row["course_code"],
                    "course_type": row["course_type"], "faculty_name": row["faculty_name"],
                    "faculty_type": row["faculty_type"],
                    "elective_group_id": row["elective_group_id"], "group_name": row["group_name"],
                }
                if key in timetable:
                    if not isinstance(timetable[key], list):
                        timetable[key] = [timetable[key]]
                    timetable[key].append(entry)
                else:
                    timetable[key] = entry

        return render_template("student.html",
                               departments=departments, timetable=timetable,
                               days=days, periods_per_day=periods_per_day,
                               selected_dept=selected_dept, selected_sem=selected_sem,
                               period_times=period_times, break_times=break_times)
    finally:
        conn.close()


# =========================================================
# FACULTY TIMETABLE
# =========================================================
@app.route("/faculty", methods=["GET", "POST"])
def faculty():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments")
        departments = cur.fetchall()

        timetable           = None
        days                = []
        periods_per_day     = 0
        selected_faculty_id = None
        selected_semester_type = "all"
        period_times        = {}
        break_times         = {}

        if request.method == "POST":
            faculty_id    = request.form.get("faculty_id")
            semester_type = request.form.get("semester_type", "all")
            selected_faculty_id    = faculty_id
            selected_semester_type = semester_type

            cur.execute("SELECT * FROM timetable_settings LIMIT 1")
            settings = cur.fetchone()
            if settings:
                periods_per_day = settings["periods_per_day"]
                working_days    = settings["working_days"] or "Mon,Tue,Wed,Thu,Fri,Sat"
                days            = [d.strip() for d in working_days.split(",")]
                start_time      = settings["start_time"] or "09:00"
                period_times, break_times = compute_period_times(
                    start_time, settings["period_duration"], periods_per_day, settings["break_details"])

            # Build semester parity filter
            if semester_type == "odd":
                sem_filter = "AND (gt.semester % 2 = 1)"
            elif semester_type == "even":
                sem_filter = "AND (gt.semester % 2 = 0)"
            else:
                sem_filter = ""

            cur.execute(f"""
                SELECT gt.day, gt.period,
                       c.course_name, c.course_code, c.course_type,
                       d.dept_name, gt.semester
                FROM generated_timetable gt
                JOIN courses c     ON gt.course_id = c.course_id
                JOIN departments d ON gt.dept_id   = d.dept_id
                WHERE gt.faculty_id=? {sem_filter}
            """, (faculty_id,))

            rows = cur.fetchall()
            timetable = {}
            for row in rows:
                timetable[(row["day"], row["period"])] = {
                    "course_name": row["course_name"], "course_code": row["course_code"],
                    "course_type": row["course_type"], "dept_name": row["dept_name"],
                    "semester": row["semester"],
                }

        return render_template("faculty_timetable.html",
                               departments=departments, timetable=timetable,
                               days=days, periods_per_day=periods_per_day,
                               selected_faculty_id=selected_faculty_id,
                               selected_semester_type=selected_semester_type,
                               period_times=period_times, break_times=break_times)
    finally:
        conn.close()


# =========================================================
# DETAILS / SETTINGS
# =========================================================
@app.route("/details", methods=["GET", "POST"])
def details():
    conn = get_db()
    try:
        cur = conn.cursor()

        if request.method == "POST":
            periods_per_day   = request.form.get("periods_per_day")
            period_duration   = request.form.get("period_duration")
            number_of_breaks  = request.form.get("number_of_breaks")
            working_days_list = request.form.getlist("working_days")
            working_days      = ",".join(working_days_list)
            start_time        = request.form.get("start_time", "09:00")

            after_periods = request.form.getlist("break_after_period")
            durations     = request.form.getlist("break_duration")
            breaks_data   = []
            for ap, dur in zip(after_periods, durations):
                if ap and dur:
                    breaks_data.append({"after_period": int(ap), "duration": int(dur)})
            break_details = json.dumps(breaks_data)

            cur.execute("DELETE FROM timetable_settings")
            cur.execute("""
                INSERT INTO timetable_settings
                (periods_per_day, period_duration, number_of_breaks, break_details, working_days, start_time)
                VALUES (?,?,?,?,?,?)
            """, (periods_per_day, period_duration, number_of_breaks, break_details, working_days, start_time))
            conn.commit()
            flash("Settings saved successfully!", "success")

        cur.execute("SELECT * FROM timetable_settings LIMIT 1")
        settings = cur.fetchone()

        cur.execute("SELECT COUNT(*) FROM departments");         total_departments       = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM faculties");           total_faculties         = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM courses");             total_courses           = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM generated_timetable"); total_timetable_entries = cur.fetchone()[0]

        saved_days = []
        if settings and settings["working_days"]:
            saved_days = [d.strip() for d in settings["working_days"].split(",")]

        saved_breaks = []
        if settings and settings["break_details"]:
            try:
                saved_breaks = json.loads(settings["break_details"])
            except Exception:
                pass

        return render_template("details.html", settings=settings,
                               total_departments=total_departments,
                               total_faculties=total_faculties,
                               total_courses=total_courses,
                               total_timetable_entries=total_timetable_entries,
                               all_days=["Mon","Tue","Wed","Thu","Fri","Sat"],
                               saved_days=saved_days, saved_breaks=saved_breaks,
                               smtp_config=get_smtp_config(),
                               base_url=BASE_URL)
    finally:
        conn.close()


# =========================================================
# SMTP
# =========================================================
@app.route("/smtp/save", methods=["POST"])
def save_smtp():
    smtp_email       = request.form.get("smtp_email", "").strip()
    smtp_password    = request.form.get("smtp_password", "").strip()
    smtp_sender_name = request.form.get("smtp_sender_name", "TimeTable Pro").strip()
    smtp_reply_to    = request.form.get("smtp_reply_to", "").strip()

    if not smtp_email or not smtp_password:
        flash("Please provide both Gmail address and App Password.", "error")
        return redirect(url_for("details"))

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM smtp_config")
        cur.execute("""
            INSERT INTO smtp_config (smtp_email, smtp_password, smtp_sender_name, smtp_reply_to, smtp_configured)
            VALUES (?,?,?,?,1)
        """, (smtp_email, smtp_password, smtp_sender_name, smtp_reply_to))
        conn.commit()
    finally:
        conn.close()
    flash("Email settings saved successfully!", "success")
    return redirect(url_for("details"))


@app.route("/smtp/test", methods=["POST"])
def test_smtp():
    smtp_cfg = get_smtp_config()
    if not smtp_cfg or not smtp_cfg["smtp_configured"]:
        return jsonify({"success": False, "error": "SMTP not configured yet."})

    success, error = send_allocation_email(
        to_email=smtp_cfg["smtp_email"],
        faculty_name="Admin (Test)",
        form_link=f"{BASE_URL}/allocation",
        semester_type="odd",
        dept_name="Test Department",
        smtp_cfg=smtp_cfg
    )
    if success:
        return jsonify({"success": True, "sent_to": smtp_cfg["smtp_email"]})
    else:
        return jsonify({"success": False, "error": error})


# =========================================================
# DEPARTMENTS
# =========================================================
@app.route("/departments", methods=["GET", "POST"])
def departments():
    conn = get_db()
    try:
        cur = conn.cursor()

        if request.method == "POST":
            dept_id   = request.form.get("dept_id")
            dept_name = request.form.get("dept_name")
            if dept_id and dept_name:
                cur.execute("SELECT dept_id FROM departments WHERE dept_id=?", (dept_id,))
                if cur.fetchone():
                    flash('Department ID already exists!', 'error')
                    return redirect(url_for("departments"))
                cur.execute("INSERT INTO departments (dept_id, dept_name) VALUES (?,?)", (dept_id, dept_name))
                conn.commit()
                flash('Department added successfully!', 'success')
            return redirect(url_for("departments"))

        cur.execute("SELECT * FROM departments")
        departments_list = cur.fetchall()
        return render_template("departments.html", departments=departments_list)
    finally:
        conn.close()


@app.route("/delete_department/<int:dept_id>")
def delete_department(dept_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM courses WHERE dept_id=?",            (dept_id,))
        cur.execute("DELETE FROM faculties WHERE dept_id=?",           (dept_id,))
        cur.execute("DELETE FROM generated_timetable WHERE dept_id=?", (dept_id,))
        cur.execute("DELETE FROM elective_options WHERE group_id IN "
                    "(SELECT group_id FROM elective_groups WHERE dept_id=?)", (dept_id,))
        cur.execute("DELETE FROM elective_groups WHERE dept_id=?",     (dept_id,))
        cur.execute("DELETE FROM departments WHERE dept_id=?",         (dept_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("departments"))


# =========================================================
# FACULTY MANAGEMENT
# =========================================================
@app.route("/faculty_home")
def faculty_home():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments")
        departments_list = cur.fetchall()
        return render_template("faculty_home.html", departments=departments_list)
    finally:
        conn.close()


@app.route("/faculties/<int:dept_id>", methods=["GET", "POST"])
def faculties(dept_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments WHERE dept_id=?", (dept_id,))
        dept = cur.fetchone()
        if not dept:
            return "Department not found"

        if request.method == "POST":
            faculty_name       = request.form.get("faculty_name")
            faculty_type       = request.form.get("faculty_type", "regular")
            faculty_email      = request.form.get("faculty_email", "").strip()
            visiting_days_list = request.form.getlist("visiting_days")
            visiting_days      = ",".join(visiting_days_list) if faculty_type == "visiting" else ""

            if faculty_name:
                cur.execute("SELECT COALESCE(MAX(seniority_rank),0)+1 FROM faculties WHERE dept_id=?", (dept_id,))
                next_rank = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO faculties (faculty_name, dept_id, faculty_type, visiting_days, seniority_rank, faculty_email) VALUES (?,?,?,?,?,?)",
                    (faculty_name, dept_id, faculty_type, visiting_days, next_rank, faculty_email)
                )
                conn.commit()

        cur.execute("SELECT * FROM faculties WHERE dept_id=? ORDER BY seniority_rank ASC", (dept_id,))
        faculties_list = cur.fetchall()
        return render_template("faculties.html",
                               faculties=faculties_list, dept_id=dept_id, dept_name=dept["dept_name"])
    finally:
        conn.close()


@app.route("/delete_faculty/<int:dept_id>/<int:faculty_id>")
def delete_faculty(dept_id, faculty_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM courses WHERE faculty_id=?",  (faculty_id,))
        cur.execute("DELETE FROM faculties WHERE faculty_id=?", (faculty_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("faculties", dept_id=dept_id))


@app.route("/faculty/update/<int:faculty_id>", methods=["POST"])
def update_faculty(faculty_id):
    data      = request.get_json()
    new_name  = (data.get("new_name")  or "").strip()
    new_email = (data.get("new_email") or "").strip()
    if not new_name or len(new_name) < 2:
        return jsonify({"success": False, "error": "Name must be at least 2 characters."})
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT faculty_id FROM faculties WHERE faculty_id=?", (faculty_id,))
        if not cur.fetchone():
            return jsonify({"success": False, "error": "Faculty not found."})
        cur.execute("UPDATE faculties SET faculty_name=?, faculty_email=? WHERE faculty_id=?",
                    (new_name, new_email, faculty_id))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


@app.route("/faculty/reorder/<int:dept_id>", methods=["POST"])
def reorder_faculty(dept_id):
    data        = request.get_json()
    ordered_ids = data.get("ordered_ids", [])
    conn = get_db()
    try:
        cur = conn.cursor()
        for rank, fid in enumerate(ordered_ids, start=1):
            cur.execute("UPDATE faculties SET seniority_rank=? WHERE faculty_id=? AND dept_id=?",
                        (rank, fid, dept_id))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


# =========================================================
# COURSES
# =========================================================
@app.route("/courses")
def courses_home():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments")
        departments_list = cur.fetchall()
        return render_template("courses_home.html", departments=departments_list)
    finally:
        conn.close()


@app.route("/courses/<int:dept_id>", methods=["GET", "POST"])
def courses(dept_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments WHERE dept_id=?", (dept_id,))
        dept = cur.fetchone()
        if not dept:
            return "Department not found"

        if request.method == "POST":
            course_name = request.form.get("course_name")
            course_code = request.form.get("course_code")
            semester    = request.form.get("semester")
            credits     = request.form.get("credits")
            course_type = request.form.get("course_type")

            if all([course_name, course_code, semester, credits, course_type]):
                cur.execute("""
                    INSERT INTO courses
                    (course_name, course_code, semester, credits, faculty_id, dept_id, course_type)
                    VALUES (?,?,?,?,NULL,?,?)
                """, (course_name, course_code, semester, credits, dept_id, course_type))
                conn.commit()
            return redirect(url_for("courses", dept_id=dept_id))

        cur.execute("""
            SELECT c.course_id, c.course_name, c.course_code, c.semester, c.credits,
                   c.course_type, f.faculty_name, c.elective_group_id, eg.group_name
            FROM courses c
            LEFT JOIN faculties f      ON c.faculty_id = f.faculty_id
            LEFT JOIN elective_groups eg ON c.elective_group_id = eg.group_id
            WHERE c.dept_id=? ORDER BY c.semester ASC, c.elective_group_id ASC
        """, (dept_id,))
        courses_list = cur.fetchall()
        return render_template("courses.html",
                               courses=courses_list, dept_id=dept_id, dept_name=dept["dept_name"])
    finally:
        conn.close()


@app.route("/delete_course/<int:course_id>/<int:dept_id>")
def delete_course(course_id, dept_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM courses WHERE course_id=?", (course_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("courses", dept_id=dept_id))


# =========================================================
# ELECTIVE GROUPS
# =========================================================
@app.route("/elective-groups")
def elective_groups_home():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments")
        departments_list = cur.fetchall()
        return render_template("elective_groups_home.html", departments=departments_list)
    finally:
        conn.close()


@app.route("/elective-groups/<int:dept_id>", methods=["GET", "POST"])
def elective_groups(dept_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments WHERE dept_id=?", (dept_id,))
        dept = cur.fetchone()
        if not dept:
            return "Department not found"

        if request.method == "POST":
            action = request.form.get("action")

            if action == "add_group":
                group_name = request.form.get("group_name", "").strip()
                semester   = request.form.get("semester")
                is_lab     = 1 if request.form.get("is_lab") == "1" else 0
                if group_name and semester:
                    cur.execute(
                        "INSERT INTO elective_groups (group_name, dept_id, semester, is_lab) VALUES (?,?,?,?)",
                        (group_name, dept_id, semester, is_lab)
                    )
                    conn.commit()
                    lab_label = " (Lab)" if is_lab else ""
                    flash(f"Elective group '{group_name}'{lab_label} created.", "success")

            elif action == "delete_group":
                group_id = request.form.get("group_id")
                cur.execute("DELETE FROM elective_options WHERE group_id=?", (group_id,))
                cur.execute("UPDATE courses SET elective_group_id=NULL WHERE elective_group_id=?", (group_id,))
                cur.execute("DELETE FROM elective_groups WHERE group_id=?", (group_id,))
                conn.commit()
                flash("Elective group deleted.", "success")

            elif action == "add_option":
                group_id    = request.form.get("group_id")
                option_name = request.form.get("option_name", "").strip()
                option_code = request.form.get("option_code", "").strip()
                credits     = request.form.get("option_credits", "3").strip()
                if group_id and option_name:
                    cur.execute("SELECT COUNT(*) FROM elective_options WHERE group_id=?", (group_id,))
                    if cur.fetchone()[0] >= 2:
                        flash("Each elective group can have at most 2 subjects.", "error")
                    else:
                        auto_code = option_code or option_name[:8].upper().replace(" ", "")
                        cur.execute(
                            "INSERT INTO elective_options (group_id, option_name, option_code, option_credits) VALUES (?,?,?,?)",
                            (group_id, option_name, auto_code, credits)
                        )
                        cur.execute("SELECT dept_id, semester, is_lab FROM elective_groups WHERE group_id=?", (group_id,))
                        grp = cur.fetchone()
                        if grp:
                            c_type = "lab" if grp["is_lab"] else "theory"
                            cur.execute("""
                                INSERT INTO courses
                                (course_name, course_code, semester, credits, faculty_id,
                                 dept_id, course_type, elective_group_id)
                                VALUES (?,?,?,?,NULL,?,?,?)
                            """, (option_name, auto_code, grp["semester"],
                                  int(credits) if credits else 3,
                                  grp["dept_id"], c_type, group_id))
                        conn.commit()
                        flash(f"Subject '{option_name}' ({auto_code}) added.", "success")

            elif action == "update_option":
                option_id      = request.form.get("option_id")
                new_name       = request.form.get("option_name", "").strip()
                new_code       = request.form.get("option_code", "").strip()
                new_credits    = request.form.get("option_credits", "3").strip()
                if option_id and new_name:
                    cur.execute("SELECT option_name, group_id FROM elective_options WHERE option_id=?", (option_id,))
                    old = cur.fetchone()
                    if old:
                        auto_code = new_code or new_name[:8].upper().replace(" ", "")
                        cur.execute("""
                            UPDATE elective_options
                            SET option_name=?, option_code=?, option_credits=?
                            WHERE option_id=?
                        """, (new_name, auto_code, new_credits, option_id))
                        cur.execute("""
                            UPDATE courses SET course_name=?, course_code=?, credits=?
                            WHERE elective_group_id=? AND course_name=?
                        """, (new_name, auto_code, int(new_credits) if new_credits else 3,
                               old["group_id"], old["option_name"]))
                        conn.commit()
                        flash(f"Subject updated to '{new_name}' ({auto_code}).", "success")

            elif action == "delete_option":
                option_id = request.form.get("option_id")
                cur.execute("SELECT option_name, option_code, group_id FROM elective_options WHERE option_id=?", (option_id,))
                opt_row = cur.fetchone()
                if opt_row:
                    cur.execute("""
                        DELETE FROM courses
                        WHERE course_name=? AND course_code=? AND elective_group_id=?
                    """, (opt_row["option_name"], opt_row["option_code"], opt_row["group_id"]))
                cur.execute("DELETE FROM elective_options WHERE option_id=?", (option_id,))
                conn.commit()
                flash("Subject removed.", "success")

            return redirect(url_for("elective_groups", dept_id=dept_id))

        cur.execute("""
            SELECT eg.* FROM elective_groups eg
            WHERE eg.dept_id=? ORDER BY eg.semester, eg.group_name
        """, (dept_id,))
        groups = cur.fetchall()

        cur.execute("SELECT DISTINCT semester FROM courses WHERE dept_id=? AND semester>=5 ORDER BY semester", (dept_id,))
        available_semesters = [r["semester"] for r in cur.fetchall()]
        if not available_semesters:
            available_semesters = list(range(5, 9))

        options_by_group = {}
        if groups:
            gids = [g["group_id"] for g in groups]
            placeholders = ",".join("?" * len(gids))
            cur.execute(
                f"SELECT option_id, group_id, option_name, option_code, option_credits "
                f"FROM elective_options WHERE group_id IN ({placeholders}) ORDER BY option_id",
                gids
            )
            for row in cur.fetchall():
                options_by_group.setdefault(row["group_id"], []).append(row)

        return render_template("elective_groups.html",
                               dept=dept, dept_id=dept_id,
                               groups=groups,
                               options_by_group=options_by_group,
                               available_semesters=available_semesters)
    finally:
        conn.close()


# =========================================================
# API
# =========================================================
@app.route("/api/faculties/<int:dept_id>")
def api_faculties(dept_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT faculty_id, faculty_name, faculty_type, visiting_days, seniority_rank
            FROM faculties WHERE dept_id=? ORDER BY seniority_rank
        """, (dept_id,))
        faculties_list = cur.fetchall()
        return jsonify([{
            "faculty_id":     f["faculty_id"],
            "faculty_name":   f["faculty_name"],
            "faculty_type":   f["faculty_type"],
            "visiting_days":  f["visiting_days"],
            "seniority_rank": f["seniority_rank"],
        } for f in faculties_list])
    finally:
        conn.close()


# =========================================================
# FACULTY ALLOCATION
# =========================================================

@app.route("/allocation/setup", methods=["GET", "POST"])
def allocation_setup():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments ORDER BY dept_name")
        departments_list = cur.fetchall()

        if request.method == "POST":
            dept_id       = request.form.get("dept_id")
            semester_type = request.form.get("semester_type", "odd")

            if not dept_id:
                flash("Please select a department.", "error")
                return redirect(url_for("allocation_setup"))

            cur.execute("SELECT * FROM departments WHERE dept_id=?", (dept_id,))
            dept = cur.fetchone()
            if not dept:
                flash("Selected department does not exist.", "error")
                return redirect(url_for("allocation_setup"))

            cur.execute("""
                SELECT faculty_id, faculty_name, seniority_rank, faculty_type
                FROM faculties WHERE dept_id=? ORDER BY seniority_rank ASC
            """, (dept_id,))
            faculties = cur.fetchall()
            total = len(faculties)

            default_quotas = {}
            for idx, f in enumerate(faculties, start=1):
                tq, lq = _quota_for_rank(idx, total)
                default_quotas[f["faculty_id"]] = (tq, lq)

            return render_template("allocation_setup.html",
                                   departments=departments_list,
                                   dept=dept, dept_id=dept_id,
                                   semester_type=semester_type,
                                   faculties=faculties,
                                   default_quotas=default_quotas)

        return render_template("allocation_setup.html",
                               departments=departments_list,
                               dept=None, dept_id=None,
                               semester_type=None,
                               faculties=None,
                               default_quotas={})
    finally:
        conn.close()


@app.route("/allocation")
def allocation_home():
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("SELECT * FROM departments ORDER BY dept_name")
        departments_list = cur.fetchall()

        cur.execute("""
            SELECT ar.*, d.dept_name,
                   COUNT(DISTINCT at2.token_id) as total_faculty,
                   SUM(CASE WHEN at2.status='done'   THEN 1 ELSE 0 END) as done_count,
                   SUM(CASE WHEN at2.status='active' THEN 1 ELSE 0 END) as active_count
            FROM allocation_rounds ar
            JOIN departments d ON ar.dept_id = d.dept_id
            LEFT JOIN allocation_tokens at2 ON at2.round_id = ar.round_id
            GROUP BY ar.round_id
            ORDER BY ar.created_at DESC
        """)
        rounds = cur.fetchall()

        return render_template("allocation_home.html",
                               departments=departments_list, rounds=rounds, base_url=BASE_URL)
    finally:
        conn.close()


@app.route("/allocation/start", methods=["POST"])
def allocation_start():
    dept_id       = request.form.get("dept_id")
    semester_type = request.form.get("semester_type", "odd")

    if semester_type not in ("odd", "even"):
        semester_type = "odd"

    if not dept_id:
        flash("Please select a department.", "error")
        return redirect(url_for("allocation_setup"))

    if BASE_URL == 'http://localhost:5000':
        flash("⚠️ BASE_URL is set to localhost — email links won't work on other devices.", "error")
        return redirect(url_for("allocation_setup"))

    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("SELECT dept_id FROM departments WHERE dept_id=?", (dept_id,))
        if not cur.fetchone():
            flash("Selected department does not exist.", "error")
            return redirect(url_for("allocation_home"))

        cur.execute("""
            SELECT round_id FROM allocation_rounds
            WHERE dept_id=? AND semester_type=? AND status='active'
        """, (dept_id, semester_type))
        if cur.fetchone():
            flash("An active round already exists for this department and semester type.", "error")
            return redirect(url_for("allocation_home"))

        cur.execute("""
            INSERT INTO allocation_rounds (dept_id, semester_type, status)
            VALUES (?, ?, 'active')
        """, (dept_id, semester_type))
        round_id = cur.lastrowid

        cur.execute("""
            SELECT faculty_id, seniority_rank FROM faculties
            WHERE dept_id=? ORDER BY seniority_rank ASC
        """, (dept_id,))
        faculty_rows = cur.fetchall()
        total = len(faculty_rows)

        for idx, fac in enumerate(faculty_rows, start=1):
            token = secrets.token_urlsafe(24)
            fid = fac["faculty_id"]
            theory_q = request.form.get(f"theory_{fid}")
            lab_q    = request.form.get(f"lab_{fid}")
            if theory_q is None or lab_q is None:
                theory_q, lab_q = _quota_for_rank(idx, total)
            else:
                try:
                    theory_q = max(0, int(theory_q))
                    lab_q    = max(0, int(lab_q))
                except (ValueError, TypeError):
                    theory_q, lab_q = _quota_for_rank(idx, total)
            status = 'active' if idx == 1 else 'waiting'
            cur.execute("""
                INSERT INTO allocation_tokens
                (round_id, faculty_id, seniority_rank, token, status, theory_quota, lab_quota)
                VALUES (?,?,?,?,?,?,?)
            """, (round_id, fid, idx, token, status, theory_q, lab_q))

        outside_fids = request.form.getlist("outside_faculty_id")
        outside_theories = request.form.getlist("outside_theory")
        outside_labs = request.form.getlist("outside_lab")

        for i, fid in enumerate(outside_fids):
            if not fid:
                continue
            cur.execute("SELECT 1 FROM allocation_tokens WHERE round_id=? AND faculty_id=?", (round_id, fid))
            if cur.fetchone():
                continue

            token = secrets.token_urlsafe(24)
            theory_q = int(outside_theories[i]) if i < len(outside_theories) and outside_theories[i].isdigit() else 1
            lab_q    = int(outside_labs[i]) if i < len(outside_labs) and outside_labs[i].isdigit() else 0

            idx = total + i + 1
            status = 'active' if idx == 1 else 'waiting'
            cur.execute("""
                INSERT INTO allocation_tokens
                (round_id, faculty_id, seniority_rank, token, status, theory_quota, lab_quota)
                VALUES (?,?,?,?,?,?,?)
            """, (round_id, fid, idx, token, status, theory_q, lab_q))

        conn.commit()

        cur.execute("""
            SELECT at2.token, f.faculty_name, f.faculty_email
            FROM allocation_tokens at2
            JOIN faculties f ON at2.faculty_id = f.faculty_id
            WHERE at2.round_id=? AND at2.status='active'
            LIMIT 1
        """, (round_id,))
        first_token = cur.fetchone()

        cur.execute("SELECT dept_name FROM departments WHERE dept_id=?", (dept_id,))
        dept_row      = cur.fetchone()
        dept_name_val = dept_row["dept_name"] if dept_row else ""

    finally:
        conn.close()

    if first_token:
        link     = f"{BASE_URL}/allocation/form/{first_token['token']}"
        smtp_cfg = get_smtp_config()
        if first_token["faculty_email"]:
            ok, err = send_allocation_email(
                first_token["faculty_email"], first_token["faculty_name"],
                link, semester_type, dept_name_val, smtp_cfg
            )
            if ok:
                flash(f"Round started! Email sent to {first_token['faculty_name']}.", "success")
            else:
                flash(f"Round started but email failed: {err}. Manual link: {link}", "error")
        else:
            flash(f"Round started! {first_token['faculty_name']} has no email. Share manually: {link}", "error")

    return redirect(url_for("allocation_round_detail", round_id=round_id))


@app.route("/allocation/round/<int:round_id>")
def allocation_round_detail(round_id):
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT ar.*, d.dept_name
            FROM allocation_rounds ar
            JOIN departments d ON ar.dept_id = d.dept_id
            WHERE ar.round_id=?
        """, (round_id,))
        round_row = cur.fetchone()
        if not round_row:
            return "Round not found", 404

        cur.execute("""
            SELECT at2.*, f.faculty_name, f.seniority_rank, f.faculty_email
            FROM allocation_tokens at2
            JOIN faculties f ON at2.faculty_id = f.faculty_id
            WHERE at2.round_id=?
            ORDER BY at2.seniority_rank ASC
        """, (round_id,))
        tokens = cur.fetchall()

        cur.execute("""
            SELECT fa.faculty_id,
                   COUNT(CASE WHEN c.course_type='theory' THEN 1 END) as theory_count,
                   COUNT(CASE WHEN c.course_type='lab'    THEN 1 END) as lab_count
            FROM faculty_allocations fa
            JOIN courses c ON fa.course_id = c.course_id
            WHERE fa.round_id=?
            GROUP BY fa.faculty_id
        """, (round_id,))
        alloc_summary = {row["faculty_id"]: row for row in cur.fetchall()}

        cur.execute("SELECT * FROM departments ORDER BY dept_name")
        all_departments = cur.fetchall()

        return render_template("allocation_round.html",
                               round=round_row, tokens=tokens,
                               alloc_summary=alloc_summary, departments=all_departments, base_url=BASE_URL)
    finally:
        conn.close()


@app.route("/allocation/round/<int:round_id>/add_outside_faculty", methods=["POST"])
def allocation_add_outside_faculty(round_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        faculty_id = request.form.get("faculty_id")
        theory_q = request.form.get("theory_quota", 1)
        lab_q = request.form.get("lab_quota", 0)

        if not faculty_id:
            flash("Please select a faculty.", "error")
            return redirect(url_for("allocation_round_detail", round_id=round_id))

        try:
            theory_q = max(0, int(theory_q))
            lab_q    = max(0, int(lab_q))
        except (ValueError, TypeError):
            theory_q, lab_q = 1, 0

        cur.execute("SELECT token_id FROM allocation_tokens WHERE round_id=? AND faculty_id=?", (round_id, faculty_id))
        if cur.fetchone():
            flash("Faculty is already in this round.", "error")
            return redirect(url_for("allocation_round_detail", round_id=round_id))

        cur.execute("SELECT MAX(seniority_rank) FROM allocation_tokens WHERE round_id=?", (round_id,))
        rv = cur.fetchone()[0]
        next_rank = (rv if rv else 0) + 1

        cur.execute("SELECT token_id FROM allocation_tokens WHERE round_id=? AND status IN ('active', 'waiting')", (round_id,))
        has_pending = cur.fetchone()
        status = 'waiting' if has_pending else 'active'

        token = secrets.token_urlsafe(24)

        cur.execute("""
            INSERT INTO allocation_tokens
            (round_id, faculty_id, seniority_rank, token, status, theory_quota, lab_quota)
            VALUES (?,?,?,?,?,?,?)
        """, (round_id, faculty_id, next_rank, token, status, theory_q, lab_q))
        conn.commit()

        cur.execute("SELECT status FROM allocation_rounds WHERE round_id=?", (round_id,))
        rstatus = cur.fetchone()["status"]
        if rstatus in ("pending_review", "closed") and status == "active":
            cur.execute("UPDATE allocation_rounds SET status='active' WHERE round_id=?", (round_id,))
            conn.commit()

        flash("Faculty added to round successfully.", "success")
    finally:
        conn.close()
    return redirect(url_for("allocation_round_detail", round_id=round_id))


@app.route("/allocation/form/<token>", methods=["GET", "POST"])
def allocation_form(token):
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT at2.*, f.faculty_name, f.faculty_email,
                   ar.dept_id, ar.semester_type, ar.round_id, ar.status as round_status
            FROM allocation_tokens at2
            JOIN faculties f          ON at2.faculty_id = f.faculty_id
            JOIN allocation_rounds ar ON at2.round_id   = ar.round_id
            WHERE at2.token=?
        """, (token,))
        token_row = cur.fetchone()

        if not token_row:
            return render_template("allocation_error.html", message="Invalid or expired link.")
        if token_row["round_status"] != "active":
            return render_template("allocation_error.html", message="This allocation round has been closed.")
        if token_row["status"] == "done":
            return render_template("allocation_error.html", message="You have already submitted your preferences. Thank you!")
        if token_row["status"] == "waiting":
            return render_template("allocation_error.html", message="Your form is not yet open. Please wait until the faculty before you submits.")

        round_id      = token_row["round_id"]
        dept_id       = token_row["dept_id"]
        semester_type = token_row["semester_type"]
        faculty_id    = token_row["faculty_id"]
        theory_q      = token_row["theory_quota"]
        lab_q         = token_row["lab_quota"]

        parity_filter = "c.semester % 2 = 1" if semester_type == "odd" else "c.semester % 2 = 0"

        if request.method == "POST":
            selected_theory = request.form.getlist("theory_courses")
            selected_labs   = request.form.getlist("lab_courses")

            for cid in selected_theory:
                cur.execute("""
                    INSERT INTO faculty_allocations (round_id, token_id, faculty_id, course_id, role)
                    VALUES (?,?,?,?,'primary')
                """, (round_id, token_row["token_id"], faculty_id, cid))

            for cid in selected_labs:
                cur.execute("""
                    SELECT COUNT(*) FROM faculty_allocations
                    WHERE round_id=? AND course_id=?
                """, (round_id, cid))
                existing_total = cur.fetchone()[0]
                if existing_total >= 3:
                    continue
                role = "primary" if existing_total == 0 else "co-teacher"
                cur.execute("""
                    INSERT INTO faculty_allocations (round_id, token_id, faculty_id, course_id, role)
                    VALUES (?,?,?,?,?)
                """, (round_id, token_row["token_id"], faculty_id, cid, role))

            cur.execute("""
                UPDATE allocation_tokens SET status='done', submitted_at=CURRENT_TIMESTAMP
                WHERE token_id=?
            """, (token_row["token_id"],))

            cur.execute("""
                SELECT token_id FROM allocation_tokens
                WHERE round_id=? AND status='waiting'
                ORDER BY seniority_rank ASC LIMIT 1
            """, (round_id,))
            next_token_row = cur.fetchone()

            if next_token_row:
                cur.execute("UPDATE allocation_tokens SET status='active' WHERE token_id=?",
                            (next_token_row["token_id"],))
                cur.execute("""
                    SELECT at2.token, f.faculty_name, f.faculty_email,
                           ar.dept_id, ar.semester_type
                    FROM allocation_tokens at2
                    JOIN faculties f ON at2.faculty_id = f.faculty_id
                    JOIN allocation_rounds ar ON at2.round_id = ar.round_id
                    WHERE at2.token_id=?
                """, (next_token_row["token_id"],))
                next_info = cur.fetchone()
            else:
                next_info = None
                cur.execute("UPDATE allocation_rounds SET status='pending_review' WHERE round_id=?", (round_id,))

            conn.commit()

            next_info_dict  = None
            faculty_name_val = token_row["faculty_name"]
            if next_info:
                next_info_dict = {
                    "token":         next_info["token"],
                    "faculty_name":  next_info["faculty_name"],
                    "faculty_email": next_info["faculty_email"],
                    "dept_id":       next_info["dept_id"],
                    "semester_type": next_info["semester_type"],
                }

            conn.close()
            conn = None

            email_sent  = False
            email_error = None
            if next_info_dict and next_info_dict["faculty_email"]:
                smtp_cfg = get_smtp_config()
                dept_conn = get_db()
                try:
                    dept_cur = dept_conn.cursor()
                    dept_cur.execute("SELECT dept_name FROM departments WHERE dept_id=?", (next_info_dict["dept_id"],))
                    dept_row = dept_cur.fetchone()
                finally:
                    dept_conn.close()

                next_link = f"{BASE_URL}/allocation/form/{next_info_dict['token']}"
                email_sent, email_error = send_allocation_email(
                    next_info_dict["faculty_email"], next_info_dict["faculty_name"],
                    next_link, next_info_dict["semester_type"],
                    dept_row["dept_name"] if dept_row else "", smtp_cfg
                )

            return render_template("allocation_submitted.html",
                                   faculty_name=faculty_name_val,
                                   next_faculty=next_info_dict,
                                   email_sent=email_sent,
                                   email_error=email_error)

        cur.execute(f"""
            SELECT c.course_id FROM faculty_allocations fa
            JOIN courses c ON fa.course_id = c.course_id
            WHERE fa.round_id=? AND c.course_type='theory' AND {parity_filter} AND c.dept_id=?
        """, (round_id, dept_id))
        taken_theory_ids = {row["course_id"] for row in cur.fetchall()}

        cur.execute(f"""
            SELECT c.course_id, c.course_name, c.course_code, c.credits, c.semester,
                   c.elective_group_id, eg.group_name
            FROM courses c
            LEFT JOIN elective_groups eg ON c.elective_group_id = eg.group_id
            WHERE c.dept_id=? AND {parity_filter} AND c.course_type='theory'
            ORDER BY c.semester ASC, c.elective_group_id ASC, c.course_name ASC
        """, (dept_id,))
        all_theory = [r for r in cur.fetchall() if r["course_id"] not in taken_theory_ids]

        cur.execute(f"""
            SELECT c.course_id, c.course_name, c.course_code, c.credits, c.semester,
                   COUNT(fa.alloc_id) as assigned_count
            FROM courses c
            LEFT JOIN faculty_allocations fa ON fa.course_id = c.course_id AND fa.round_id=?
            WHERE c.dept_id=? AND {parity_filter} AND c.course_type='lab'
            GROUP BY c.course_id
            ORDER BY c.semester ASC, c.course_name ASC
        """, (round_id, dept_id))
        all_labs = cur.fetchall()

        cur.execute("""
            SELECT course_id FROM faculty_allocations
            WHERE round_id=? AND faculty_id=?
        """, (round_id, faculty_id))
        already_picked = {r["course_id"] for r in cur.fetchall()}

        return render_template("allocation_form.html",
                               token=token,
                               faculty_name=token_row["faculty_name"],
                               theory_quota=theory_q,
                               lab_quota=lab_q,
                               semester_type=semester_type,
                               all_theory=all_theory,
                               all_labs=all_labs,
                               already_picked=already_picked)
    finally:
        if conn is not None:
            conn.close()


@app.route("/allocation/round/<int:round_id>/confirm", methods=["POST"])
def allocation_confirm(round_id):
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("SELECT * FROM allocation_rounds WHERE round_id=?", (round_id,))
        round_row = cur.fetchone()
        if not round_row:
            flash("Round not found.", "error")
            return redirect(url_for("allocation_home"))

        if round_row["status"] == "confirmed":
            flash("This round has already been confirmed.", "error")
            return redirect(url_for("allocation_round_detail", round_id=round_id))

        force_confirm = (round_row["status"] == "active")

        cur.execute("""
            SELECT fa.course_id, fa.faculty_id, fa.role, c.course_type,
                   at2.seniority_rank
            FROM faculty_allocations fa
            JOIN allocation_tokens at2 ON fa.token_id = at2.token_id
            JOIN courses c ON fa.course_id = c.course_id
            WHERE fa.round_id=? AND fa.role='primary'
            ORDER BY at2.seniority_rank ASC
        """, (round_id,))
        all_primary = cur.fetchall()

        if not all_primary:
            flash("No course allocations found for this round. Ensure faculty have submitted.", "error")
            return redirect(url_for("allocation_round_detail", round_id=round_id))

        course_to_faculty = {}
        course_type_map   = {}
        for row in all_primary:
            cid = row["course_id"]
            if cid not in course_to_faculty:
                course_to_faculty[cid] = row["faculty_id"]
                course_type_map[cid]   = row["course_type"]

        theory_updated = 0
        lab_updated    = 0
        for course_id, faculty_id in course_to_faculty.items():
            cur.execute("UPDATE courses SET faculty_id=? WHERE course_id=?",
                        (faculty_id, course_id))
            if course_type_map.get(course_id) == "theory":
                theory_updated += 1
            else:
                lab_updated += 1

        cur.execute("UPDATE allocation_rounds SET status='confirmed' WHERE round_id=?", (round_id,))
        conn.commit()
    finally:
        conn.close()

    if force_confirm:
        flash(
            f"Force-confirmed! Note: not all faculty had submitted. "
            f"{theory_updated} theory course(s) and {lab_updated} lab course(s) assigned.",
            "info"
        )
    else:
        flash(
            f"Allocation confirmed! {theory_updated} theory course(s) and "
            f"{lab_updated} lab course(s) assigned to faculty.",
            "success"
        )
    return redirect(url_for("allocation_round_detail", round_id=round_id))


@app.route("/allocation/round/<int:round_id>/close", methods=["POST"])
def allocation_close(round_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE allocation_rounds SET status='closed' WHERE round_id=?", (round_id,))
        conn.commit()
    finally:
        conn.close()
    flash("Round closed.", "success")
    return redirect(url_for("allocation_home"))


@app.route("/allocation/round/<int:round_id>/delete", methods=["POST"])
def allocation_delete(round_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM faculty_allocations WHERE round_id=?", (round_id,))
        cur.execute("DELETE FROM allocation_tokens WHERE round_id=?", (round_id,))
        cur.execute("DELETE FROM allocation_rounds WHERE round_id=?", (round_id,))
        conn.commit()
        flash("Allocation round deleted successfully.", "success")
    except Exception as e:
        flash(f"Could not delete round: {e}", "error")
    finally:
        conn.close()
    return redirect(url_for("allocation_home"))


@app.route("/allocation/round/<int:round_id>/resend/<int:token_id>")
def allocation_resend(round_id, token_id):
    row_data = None
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT at2.token, f.faculty_name, f.faculty_email, ar.dept_id, ar.semester_type
            FROM allocation_tokens at2
            JOIN faculties f ON at2.faculty_id = f.faculty_id
            JOIN allocation_rounds ar ON at2.round_id = ar.round_id
            WHERE at2.token_id=? AND at2.round_id=?
        """, (token_id, round_id))
        row = cur.fetchone()
        if row:
            row_data = dict(row)
            if row_data["faculty_email"]:
                cur.execute("SELECT dept_name FROM departments WHERE dept_id=?", (row_data["dept_id"],))
                dept_row = cur.fetchone()
                row_data["dept_name"] = dept_row["dept_name"] if dept_row else ""
    finally:
        conn.close()

    if row_data and row_data.get("faculty_email"):
        link     = f"{BASE_URL}/allocation/form/{row_data['token']}"
        smtp_cfg = get_smtp_config()
        ok, err = send_allocation_email(
            row_data["faculty_email"], row_data["faculty_name"],
            link, row_data["semester_type"], row_data["dept_name"], smtp_cfg
        )
        if ok:
            flash(f"Email resent to {row_data['faculty_name']}.", "success")
        else:
            flash(f"Could not resend: {err}. Manual link: {link}", "error")
    elif row_data:
        flash(f"No email for {row_data['faculty_name']}. Manual link: {BASE_URL}/allocation/form/{row_data['token']}", "error")

    return redirect(url_for("allocation_round_detail", round_id=round_id))
# =========================================================
# TIMETABLE GENERATION
# =========================================================
def generate_timetable_logic(selected_dept_ids, semester_type="odd"):
    """
    RULES
    =====
    LABS (placed first, Phase 1):
      - Each lab occupies 2 consecutive periods (p, p+1).
      - Labs NEVER start at period 1 or 2.
      - Preferred start: period 3 or 4. Fallback: period 5+.
      - No faculty teaches at p-1 or p2+1 around the block (relaxed as last resort).
      - At most 2 distinct lab courses per day per semester.

    THEORY (placed after all labs, Phase 2):
      - STRICT: Free periods at morning (periods 1 & 2) are NEVER allowed for students.
        Theory placement uses a morning-first waterfall:
          Tier 1: periods 1-2 (morning), no-continuous enforced  ← always filled first
          Tier 2: periods 3-4 (mid-early), no-continuous enforced
          Tier 3: periods 5 to N-2,  no-continuous enforced
          Tier 4: periods 1-4,       relax no-continuous  (fallback)
          Tier 5: periods 5..N-2,    relax no-continuous
          Tier 6: last 2 periods,    relax no-continuous  (absolute last resort)
      - Candidates sorted by ascending period number so period 1 is ALWAYS
        chosen before period 2, before period 3, etc. No free morning gaps.
      - No faculty consecutive theory rule still enforced (relaxed only as last resort).

    POST-PLACEMENT OPTIMISATION (Phase 3):
      - Scan every entry placed in the last 2 periods.
      - For each such entry, check every earlier free slot (periods 1 to N-2)
        on the same day for the same dept+semester.
      - If the faculty is free at that earlier period AND moving there does NOT
        create a continuous run for that faculty → move the entry.
      - This ensures last 2 periods are only used when truly unavoidable.
    """
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("SELECT * FROM timetable_settings LIMIT 1")
        settings = cur.fetchone()
        if not settings:
            return False, "No timetable settings found."

        periods_per_day  = settings["periods_per_day"]
        working_days_str = settings["working_days"] or "Mon,Tue,Wed,Thu,Fri,Sat"
        days             = [d.strip() for d in working_days_str.split(",")]

        # Periods that have a break immediately after them
        break_after_periods = set()
        if settings["break_details"]:
            try:
                for b in json.loads(settings["break_details"]):
                    break_after_periods.add(int(b["after_period"]))
            except Exception:
                pass

        # Faculty availability
        faculty_available_days = {}
        cur.execute("SELECT faculty_id, faculty_type, visiting_days FROM faculties")
        for row in cur.fetchall():
            ftype = row["faculty_type"] or "regular"
            if ftype == "visiting" and row["visiting_days"]:
                allowed = set(d.strip() for d in row["visiting_days"].split(",") if d.strip())
                faculty_available_days[row["faculty_id"]] = allowed & set(days)
            else:
                faculty_available_days[row["faculty_id"]] = set(days)

        # Global faculty-busy grid
        faculty_busy = {
            day: {p: set() for p in range(1, periods_per_day + 1)}
            for day in days
        }

        # Delete entries being regenerated
        for dept_id in selected_dept_ids:
            if semester_type == "odd":
                cur.execute("DELETE FROM generated_timetable WHERE dept_id=? AND semester%2=1", (dept_id,))
            else:
                cur.execute("DELETE FROM generated_timetable WHERE dept_id=? AND semester%2=0", (dept_id,))
        conn.commit()

        # Pre-load remaining entries
        cur.execute("SELECT day, period, faculty_id FROM generated_timetable")
        for row in cur.fetchall():
            d, p, fid = row["day"], row["period"], row["faculty_id"]
            if d in faculty_busy and p in faculty_busy[d]:
                faculty_busy[d][p].add(fid)

        entries_to_insert = []   # (dept_id, semester, day, period, course_id, faculty_id)

        # ── Period constants ───────────────────────────────────────────────────
        PREF_END    = 4
        LATE_START  = PREF_END + 1
        # Last 2 periods of the day — least preferred
        LAST2_START = periods_per_day - 1   # e.g. period 5 if 6 periods total
        # Split early periods: fill morning slots (1-2) strictly before mid (3-4)
        morning_periods = list(range(1, min(3, PREF_END + 1)))              # [1,2]
        mid_periods     = list(range(3, PREF_END + 1))                      # [3,4]
        early_periods   = list(range(1, PREF_END + 1))                      # [1..4]
        late_periods    = list(range(LATE_START, periods_per_day + 1))      # [5,6]
        pre_late        = list(range(1, LAST2_START))                       # [1..N-2]

        # ── Helpers ────────────────────────────────────────────────────────────

        def is_faculty_free(fid, day, p):
            return fid not in faculty_busy[day].get(p, set())

        def is_continuous(fid, day, p):
            """
            True if placing fid at (day,p) creates a back-to-back run with an
            already-placed theory period for the same faculty on the same day.
            A break between p-1 and p (or p and p+1) means NOT continuous.
            """
            before = (
                p > 1 and
                (p - 1) not in break_after_periods and
                fid in faculty_busy[day].get(p - 1, set())
            )
            after = (
                p not in break_after_periods and
                fid in faculty_busy[day].get(p + 1, set())
            )
            return before or after

        def mark_busy(fid, day, p):
            faculty_busy[day][p].add(fid)

        def unmark_busy(fid, day, p):
            faculty_busy[day][p].discard(fid)

        def already_on_day(course_id, dept_id, semester, day):
            return any(
                e[4] == course_id
                for e in entries_to_insert
                if e[0] == dept_id and e[1] == semester and e[2] == day
            )

        # ══════════════════════════════════════════════════════════════════════
        # LAB PLACEMENT
        # ══════════════════════════════════════════════════════════════════════

        def place_lab(course_id, primary_fid, co_fids, slot_taken,
                      dept_id, semester, lab_per_day):
            if primary_fid is None:
                return False

            all_fids   = [primary_fid] + co_fids
            avail_days = [d for d in days
                          if d in faculty_available_days.get(primary_fid, set(days))]

            def lab_cands(p_min, p_max, strict=True):
                cands = []
                for day in avail_days:
                    if already_on_day(course_id, dept_id, semester, day):
                        continue
                    if len(lab_per_day[day]) >= 2:
                        continue
                    for p in range(p_min, p_max + 1):
                        p2 = p + 1
                        if p2 > periods_per_day:
                            continue
                        if p in break_after_periods:
                            continue
                        if slot_taken[day][p] or slot_taken[day][p2]:
                            continue
                        if any(not is_faculty_free(f, day, p) or
                               not is_faculty_free(f, day, p2)
                               for f in all_fids):
                            continue
                        if strict:
                            blocked = False
                            for f in all_fids:
                                if (p > 1 and
                                        (p - 1) not in break_after_periods and
                                        f in faculty_busy[day].get(p - 1, set())):
                                    blocked = True; break
                                if (p2 not in break_after_periods and
                                        f in faculty_busy[day].get(p2 + 1, set())):
                                    blocked = True; break
                            if blocked:
                                continue
                        cands.append((len(lab_per_day[day]), p, day))
                return cands

            _lmax = periods_per_day - 1
            cands = lab_cands(3, PREF_END, strict=True)
            if not cands: cands = lab_cands(3, PREF_END, strict=False)
            if not cands: cands = lab_cands(LATE_START, _lmax, strict=True)
            if not cands: cands = lab_cands(LATE_START, _lmax, strict=False)
            if not cands: return False

            cands.sort(key=lambda x: (x[0], x[1]))
            _, p, day = cands[0]
            p2 = p + 1

            slot_taken[day][p]  = True
            slot_taken[day][p2] = True
            for f in all_fids:
                mark_busy(f, day, p)
                mark_busy(f, day, p2)
                entries_to_insert.append((dept_id, semester, day, p,  course_id, f))
                entries_to_insert.append((dept_id, semester, day, p2, course_id, f))
            lab_per_day[day].add(course_id)
            return True

        # ══════════════════════════════════════════════════════════════════════
        # THEORY PLACEMENT
        # ══════════════════════════════════════════════════════════════════════

        def place_theory(course_id, faculty_id, slot_taken, dept_id, semester):
            """
            6-tier waterfall (morning-first to prevent free periods 1 & 2):
              Tier 1: periods 1-2 (morning),  no-continuous enforced   ← fill first
              Tier 2: periods 3-4 (mid-early), no-continuous enforced
              Tier 3: periods 5 to N-2,       no-continuous enforced
              Tier 4: periods 1-4,            relax no-continuous (fallback)
              Tier 5: periods 5..N-2,         relax no-continuous
              Tier 6: last 2 periods,         relax no-continuous (absolute last resort)

            Candidates are sorted by ascending period number (primary) so that
            period 1 is always chosen before period 2, ensuring no free morning gaps.
            Last 2 periods are only used in Tier 6.
            No-continuous: faculty must not already be teaching at p-1 or p+1
            on the same day (unless a break separates them).
            """
            if faculty_id is None:
                return False

            avail_days = [d for d in days
                          if d in faculty_available_days.get(faculty_id, set(days))]

            def find_slot(period_list, check_continuous=True):
                cands = []
                for day in avail_days:
                    if already_on_day(course_id, dept_id, semester, day):
                        continue
                    for p in period_list:
                        if p < 1 or p > periods_per_day:
                            continue
                        if slot_taken[day].get(p, False):
                            continue
                        if not is_faculty_free(faculty_id, day, p):
                            continue
                        if check_continuous and is_continuous(faculty_id, day, p):
                            continue
                        # Sort key: period ascending (primary) so period 1 before 2, etc.
                        cands.append((p, day))
                return cands

            # Tier 1: periods 1-2 (morning) — strictly fill before anything else
            cands = find_slot(morning_periods, check_continuous=True)
            if not cands:
                # Tier 2: periods 3-4 (mid-early), no-continuous
                cands = find_slot(mid_periods, check_continuous=True)
            if not cands:
                # Tier 3: periods 5..N-2, no-continuous
                cands = find_slot(pre_late, check_continuous=True)
            if not cands:
                # Tier 4: periods 1-4, relax no-continuous (fallback)
                cands = find_slot(early_periods, check_continuous=False)
            if not cands:
                # Tier 5: periods 5..N-2, relax no-continuous
                cands = find_slot(pre_late, check_continuous=False)
            if not cands:
                # Tier 6: last 2 periods — absolute last resort
                last2 = list(range(LAST2_START, periods_per_day + 1))
                cands = find_slot(last2, check_continuous=True)
            if not cands:
                last2 = list(range(LAST2_START, periods_per_day + 1))
                cands = find_slot(last2, check_continuous=False)
            if not cands:
                return False

            cands.sort(key=lambda x: (x[0], x[1]))
            p, day = cands[0]

            slot_taken[day][p] = True
            mark_busy(faculty_id, day, p)
            entries_to_insert.append((dept_id, semester, day, p, course_id, faculty_id))
            return True

        # ══════════════════════════════════════════════════════════════════════
        # ELECTIVE LAB PLACEMENT
        # ══════════════════════════════════════════════════════════════════════

        def place_elective_lab(assignable, slot_taken, dept_id, semester,
                               lab_per_day, common_days):
            all_fids = [c["faculty_id"] for c in assignable]
            _lmax    = periods_per_day - 1

            def eg_lab_cands(p_min, p_max, strict=True):
                cands = []
                for day in [d for d in days if d in common_days]:
                    if len(lab_per_day[day]) >= 2:
                        continue
                    if already_on_day(assignable[0]["course_id"], dept_id, semester, day):
                        continue
                    for p in range(p_min, p_max + 1):
                        p2 = p + 1
                        if p2 > periods_per_day:
                            continue
                        if p in break_after_periods:
                            continue
                        if slot_taken[day][p] or slot_taken[day][p2]:
                            continue
                        if any(not is_faculty_free(f, day, p) or
                               not is_faculty_free(f, day, p2)
                               for f in all_fids):
                            continue
                        if strict:
                            blocked = False
                            for f in all_fids:
                                if (p > 1 and
                                        (p - 1) not in break_after_periods and
                                        f in faculty_busy[day].get(p - 1, set())):
                                    blocked = True; break
                                if (p2 not in break_after_periods and
                                        f in faculty_busy[day].get(p2 + 1, set())):
                                    blocked = True; break
                            if blocked:
                                continue
                        cands.append((len(lab_per_day[day]), p, day))
                return cands

            cands = eg_lab_cands(3, PREF_END, strict=True)
            if not cands: cands = eg_lab_cands(3, PREF_END, strict=False)
            if not cands: cands = eg_lab_cands(LATE_START, _lmax, strict=True)
            if not cands: cands = eg_lab_cands(LATE_START, _lmax, strict=False)
            if not cands: return False

            cands.sort(key=lambda x: (x[0], x[1]))
            _, p, day = cands[0]
            p2 = p + 1

            slot_taken[day][p]  = True
            slot_taken[day][p2] = True
            for c in assignable:
                f = c["faculty_id"]
                mark_busy(f, day, p)
                mark_busy(f, day, p2)
                entries_to_insert.append((dept_id, semester, day, p,  c["course_id"], f))
                entries_to_insert.append((dept_id, semester, day, p2, c["course_id"], f))
            lab_per_day[day].add(assignable[0]["course_id"])
            return True

        # ══════════════════════════════════════════════════════════════════════
        # ELECTIVE THEORY PLACEMENT
        # ══════════════════════════════════════════════════════════════════════

        def place_elective_theory(assignable, slot_taken, dept_id, semester,
                                  all_allowed):
            all_fids = [c["faculty_id"] for c in assignable]

            def find_slot(period_list, check_continuous=True):
                cands = []
                for day in [d for d in days if d in all_allowed]:
                    if already_on_day(assignable[0]["course_id"], dept_id, semester, day):
                        continue
                    for p in period_list:
                        if p < 1 or p > periods_per_day:
                            continue
                        if slot_taken[day].get(p, False):
                            continue
                        if any(f in faculty_busy[day][p] for f in all_fids):
                            continue
                        if check_continuous and any(
                            is_continuous(f, day, p) for f in all_fids
                        ):
                            continue
                        # Sort key: period ascending so period 1 before 2, etc.
                        cands.append((p, day))
                return cands

            # Tier 1: periods 1-2 (morning) — fill before anything else
            cands = find_slot(morning_periods, check_continuous=True)
            if not cands:
                # Tier 2: periods 3-4, no-continuous
                cands = find_slot(mid_periods, check_continuous=True)
            if not cands:
                cands = find_slot(pre_late, check_continuous=True)
            if not cands:
                cands = find_slot(early_periods, check_continuous=False)
            if not cands:
                cands = find_slot(pre_late, check_continuous=False)
            if not cands:
                last2 = list(range(LAST2_START, periods_per_day + 1))
                cands = find_slot(last2, check_continuous=True)
            if not cands:
                last2 = list(range(LAST2_START, periods_per_day + 1))
                cands = find_slot(last2, check_continuous=False)
            if not cands:
                return False

            cands.sort(key=lambda x: (x[0], x[1]))
            p, day = cands[0]

            slot_taken[day][p] = True
            for c in assignable:
                f = c["faculty_id"]
                mark_busy(f, day, p)
                entries_to_insert.append((dept_id, semester, day, p, c["course_id"], f))
            return True

        # ══════════════════════════════════════════════════════════════════════
        # MAIN SCHEDULING LOOP
        # ══════════════════════════════════════════════════════════════════════

        # slot_taken_all[dept_id][semester][day][period] — kept across semesters
        # so we can do the post-placement swap correctly
        slot_taken_all = {}

        for dept_id in selected_dept_ids:
            slot_taken_all[dept_id] = {}

            cur.execute(
                "SELECT DISTINCT semester FROM courses WHERE dept_id=?", (dept_id,)
            )
            all_sems  = [row["semester"] for row in cur.fetchall()]
            semesters = [
                s for s in all_sems
                if (s % 2 == 1 if semester_type == "odd" else s % 2 == 0)
            ]

            for semester in semesters:
                slot_taken = {
                    day: {p: False for p in range(1, periods_per_day + 1)}
                    for day in days
                }
                slot_taken_all[dept_id][semester] = slot_taken
                lab_per_day = {day: set() for day in days}

                cur.execute("""
                    SELECT course_id, credits, faculty_id, course_type, elective_group_id
                    FROM courses WHERE dept_id=? AND semester=?
                """, (dept_id, semester))
                courses_list = list(cur.fetchall())
                if not courses_list:
                    continue

                cur.execute("""
                    SELECT fa.course_id, fa.faculty_id
                    FROM faculty_allocations fa
                    JOIN allocation_rounds ar ON fa.round_id = ar.round_id
                    WHERE ar.dept_id=? AND ar.status='confirmed' AND fa.role='co-teacher'
                """, (dept_id,))
                co_map = {}
                for row in cur.fetchall():
                    co_map.setdefault(row["course_id"], []).append(row["faculty_id"])

                cur.execute("""
                    SELECT group_id, is_lab FROM elective_groups
                    WHERE dept_id=? AND semester=?
                """, (dept_id, semester))
                eg_meta = {r["group_id"]: bool(r["is_lab"]) for r in cur.fetchall()}
                scheduled_eg = set()

                lab_list    = []
                theory_list = []
                eg_courses  = {}

                for course in courses_list:
                    gid = course["elective_group_id"]
                    if gid:
                        eg_courses.setdefault(gid, []).append(course)
                        continue
                    credits = course["credits"] or 1
                    if course["course_type"] == "lab":
                        for _ in range(credits):
                            lab_list.append(course)
                    else:
                        for _ in range(credits):
                            theory_list.append(course)

                # Phase 1: Labs
                random.shuffle(lab_list)
                for lab in lab_list:
                    fid = lab["faculty_id"]
                    co  = co_map.get(lab["course_id"], [])[:]
                    if fid is None and co:
                        fid = co.pop(0)
                    place_lab(lab["course_id"], fid, co,
                              slot_taken, dept_id, semester, lab_per_day)

                for gid, grp_courses in eg_courses.items():
                    if not eg_meta.get(gid, False):
                        continue
                    if gid in scheduled_eg:
                        continue
                    assignable = [c for c in grp_courses if c["faculty_id"] is not None]
                    if not assignable:
                        scheduled_eg.add(gid); continue

                    credits     = assignable[0]["credits"] or 1
                    all_fids    = [c["faculty_id"] for c in assignable]
                    common_days = set.intersection(
                        *[faculty_available_days.get(f, set(days)) for f in all_fids]
                    ) if all_fids else set(days)

                    for _ in range(credits):
                        place_elective_lab(assignable, slot_taken, dept_id,
                                           semester, lab_per_day, common_days)
                    scheduled_eg.add(gid)

                # Phase 2: Theory
                random.shuffle(theory_list)
                for theory in theory_list:
                    place_theory(theory["course_id"], theory["faculty_id"],
                                 slot_taken, dept_id, semester)

                for gid, grp_courses in eg_courses.items():
                    if eg_meta.get(gid, False):
                        continue
                    if gid in scheduled_eg:
                        continue
                    assignable = [c for c in grp_courses if c["faculty_id"] is not None]
                    if not assignable:
                        scheduled_eg.add(gid); continue

                    credits     = assignable[0]["credits"] or 1
                    all_fids    = [c["faculty_id"] for c in assignable]
                    all_allowed = set.intersection(
                        *[faculty_available_days.get(f, set(days)) for f in all_fids]
                    ) if all_fids else set(days)

                    for _ in range(credits):
                        place_elective_theory(assignable, slot_taken, dept_id,
                                              semester, all_allowed)
                    scheduled_eg.add(gid)

        # ══════════════════════════════════════════════════════════════════════
        # PHASE 3: POST-PLACEMENT SWAP — move any entry in period 3+ earlier
        # ══════════════════════════════════════════════════════════════════════
        # For every theory entry NOT in periods 1 or 2, try to pull it into
        # period 1 or 2 if a free morning slot exists where:
        #   (a) the student slot is free  (slot_taken is False)
        #   (b) the faculty is free at that period
        #   (c) moving there does NOT create a continuous run for the faculty
        # This is the safety net ensuring periods 1 & 2 are never left free
        # when there are courses still to be placed.
        # We ALSO keep the original last-2-period pull-forward logic.
        # Process in ascending period order so period 3 is moved before period 4.

        if periods_per_day >= 3:
            last2_set = set(range(LAST2_START, periods_per_day + 1))

            # Collect indices of ALL non-lab entries not already in period 1 or 2
            swap_candidates = [
                i for i, e in enumerate(entries_to_insert)
                if e[3] > 2   # period > 2
            ]
            # Sort: process lower periods first (3 before 4, 4 before 5, ...)
            swap_candidates.sort(key=lambda i: entries_to_insert[i][3])

            for idx in swap_candidates:
                e = entries_to_insert[idx]
                dept_id_e, sem_e, day_e, p_old, course_id_e, fid_e = e

                # Skip lab entries (they come in consecutive pairs — don't move them)
                is_lab_entry = any(
                    (oe[0] == dept_id_e and oe[1] == sem_e and
                     oe[2] == day_e and oe[4] == course_id_e and
                     oe[5] == fid_e and abs(oe[3] - p_old) == 1)
                    for oe in entries_to_insert
                )
                if is_lab_entry:
                    continue

                slot_taken_s = slot_taken_all.get(dept_id_e, {}).get(sem_e)
                if slot_taken_s is None:
                    continue

                # Determine target range:
                # If the entry is in the last-2 zone, try all earlier periods (1..N-2).
                # Otherwise try only morning periods (1-2) to compact mornings.
                if p_old in last2_set:
                    target_range = range(1, LAST2_START)
                else:
                    target_range = range(1, 3)  # only try to pull into period 1 or 2

                best_p = None
                for p_new in target_range:
                    if p_new >= p_old:          # only move to earlier period
                        break
                    if p_new > periods_per_day:
                        break
                    # Student slot must be free
                    if slot_taken_s[day_e].get(p_new, True):
                        continue
                    # Faculty must be free
                    if not is_faculty_free(fid_e, day_e, p_new):
                        continue
                    # Temporarily unmark old position to check continuity cleanly
                    unmark_busy(fid_e, day_e, p_old)
                    continuous = is_continuous(fid_e, day_e, p_new)
                    mark_busy(fid_e, day_e, p_old)   # restore
                    if continuous:
                        continue
                    best_p = p_new
                    break   # take the earliest valid slot

                if best_p is not None:
                    # Perform the swap
                    unmark_busy(fid_e, day_e, p_old)
                    mark_busy(fid_e, day_e, best_p)
                    slot_taken_s[day_e][p_old]  = False
                    slot_taken_s[day_e][best_p] = True
                    entries_to_insert[idx] = (
                        dept_id_e, sem_e, day_e, best_p, course_id_e, fid_e
                    )

        # Deduplicate then bulk-insert
        seen    = set()
        deduped = []
        for e in entries_to_insert:
            key = (e[0], e[1], e[2], e[3], e[4], e[5])
            if key not in seen:
                seen.add(key)
                deduped.append(e)

        cur.executemany("""
            INSERT INTO generated_timetable (dept_id, semester, day, period, course_id, faculty_id)
            VALUES (?,?,?,?,?,?)
        """, deduped)
        conn.commit()
        return True, (
            f"{'Odd' if semester_type == 'odd' else 'Even'} semester timetable generated "
            f"for {len(selected_dept_ids)} department(s). {len(deduped)} slots assigned."
        )
    finally:
        conn.close()


@app.route("/generate-timetable", methods=["GET", "POST"])
def generate_timetable():
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM departments"); dept_count   = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM faculties");   fac_count    = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM courses");     course_count = cur.fetchone()[0]

        if dept_count == 0 or fac_count == 0 or course_count == 0:
            flash("Please set up departments, faculties, and courses before generating.", "error")
            return redirect(url_for("departments"))

        result_message    = None
        result_type       = None
        preview           = {}
        selected_dept_ids = []
        semester_type     = "odd"

        cur.execute("SELECT * FROM timetable_settings LIMIT 1")
        settings = cur.fetchone()

        days, periods_per_day, period_times, break_times = [], 0, {}, {}
        if settings:
            periods_per_day = settings["periods_per_day"]
            working_days    = settings["working_days"] or "Mon,Tue,Wed,Thu,Fri,Sat"
            days            = [d.strip() for d in working_days.split(",")]
            start_time      = settings["start_time"] or "09:00"
            period_times, break_times = compute_period_times(
                start_time, settings["period_duration"], periods_per_day, settings["break_details"])

        if request.method == "POST":
            selected_dept_ids = [int(x) for x in request.form.getlist("selected_depts")]
            semester_type     = request.form.get("semester_type", "odd")
            if not selected_dept_ids:
                result_message = "Please select at least one department."
                result_type    = "error"
            else:
                conn.close()
                conn = None
                success, message = generate_timetable_logic(selected_dept_ids, semester_type)
                result_message = message
                result_type    = "success" if success else "error"
                conn = get_db()
                cur  = conn.cursor()

                cur.execute("SELECT * FROM timetable_settings LIMIT 1")
                settings = cur.fetchone()
                if settings:
                    periods_per_day = settings["periods_per_day"]
                    working_days    = settings["working_days"] or "Mon,Tue,Wed,Thu,Fri,Sat"
                    days            = [d.strip() for d in working_days.split(",")]
                    start_time      = settings["start_time"] or "09:00"
                    period_times, break_times = compute_period_times(
                        start_time, settings["period_duration"], periods_per_day, settings["break_details"])

        cur.execute("SELECT * FROM departments")
        all_departments = cur.fetchall()

        for dept in all_departments:
            did = dept["dept_id"]
            preview[did] = {"dept_name": dept["dept_name"], "semesters": {}}

            cur.execute("SELECT DISTINCT semester FROM generated_timetable WHERE dept_id=? ORDER BY semester", (did,))
            semesters = [row["semester"] for row in cur.fetchall()]

            for sem in semesters:
                grid = {day: {p: [] for p in range(1, periods_per_day + 1)} for day in days}
                cur.execute("""
                    SELECT gt.day, gt.period,
                           c.course_name, c.course_code, c.course_type, c.elective_group_id,
                           f.faculty_name, f.faculty_type, eg.group_name
                    FROM generated_timetable gt
                    JOIN courses c   ON gt.course_id  = c.course_id
                    JOIN faculties f ON gt.faculty_id = f.faculty_id
                    LEFT JOIN elective_groups eg ON c.elective_group_id = eg.group_id
                    WHERE gt.dept_id=? AND gt.semester=?
                """, (did, sem))
                for row in cur.fetchall():
                    if row["day"] in grid:
                        grid[row["day"]][row["period"]].append({
                            "course_name": row["course_name"], "course_code": row["course_code"],
                            "course_type": row["course_type"], "faculty_name": row["faculty_name"],
                            "faculty_type": row["faculty_type"],
                            "elective_group_id": row["elective_group_id"], "group_name": row["group_name"],
                        })
                preview[did]["semesters"][sem] = grid

        return render_template("generate_timetable.html",
                               result_message=result_message, result_type=result_type,
                               preview=preview, days=days, periods_per_day=periods_per_day,
                               settings=settings, all_departments=all_departments,
                               selected_dept_ids=selected_dept_ids,
                               period_times=period_times, break_times=break_times,
                               semester_type=semester_type)
    finally:
        if conn is not None:
            conn.close()


# =========================================================
# EXAM TIMETABLE
# =========================================================
def build_exam_schedule(courses, start_date_str, exams_per_day, sessions):
    from datetime import date, timedelta
    DAY_NAMES = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    try:
        parts   = start_date_str.split("-")
        current = date(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        current = date.today()

    slots = list(sessions) if sessions else ["FN"]
    if int(exams_per_day) == 1:
        slots = [slots[0]]

    schedule = []
    queue    = list(courses)
    while queue:
        if current.weekday() == 6:
            current += timedelta(days=1)
            continue
        day_label  = DAY_NAMES[current.weekday()]
        date_label = current.strftime("%d %b %Y")
        for session in slots:
            if not queue:
                break
            course = queue.pop(0)
            schedule.append({
                "date": date_label, "day": day_label, "session": session,
                "course_name": course["course_name"], "course_code": course["course_code"],
                "course_type": course["course_type"],
            })
        current += timedelta(days=1)
    return schedule


@app.route("/exam-timetable", methods=["GET", "POST"])
def exam_timetable():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM departments ORDER BY dept_name")
        departments_list = cur.fetchall()

        exam_schedule = None
        dept_name     = ""
        submitted     = False
        selected = {
            "dept_id": "", "semester": "", "start_date": "",
            "exams_per_day": "1", "exam_type": "theory", "sessions": ["FN"],
        }

        if request.method == "POST":
            submitted     = True
            dept_id       = request.form.get("dept_id", "")
            semester      = request.form.get("semester", "")
            start_date    = request.form.get("start_date", "")
            exams_per_day = request.form.get("exams_per_day", "1")
            exam_type     = request.form.get("exam_type", "theory")
            sessions      = request.form.getlist("sessions")

            selected = {
                "dept_id": dept_id, "semester": semester, "start_date": start_date,
                "exams_per_day": exams_per_day, "exam_type": exam_type,
                "sessions": sessions if sessions else ["FN"],
            }

            if not sessions:
                flash("Please select at least one session — FN or AN.", "error")
            elif not dept_id or not semester or not start_date:
                flash("Please fill in all required fields.", "error")
            else:
                r = cur.execute("SELECT dept_name FROM departments WHERE dept_id=?", (dept_id,)).fetchone()
                dept_name = r["dept_name"] if r else ""

                if exam_type == "theory":
                    cur.execute("SELECT DISTINCT course_name, course_code, course_type FROM courses "
                                "WHERE dept_id=? AND semester=? AND course_type='theory' ORDER BY course_name",
                                (dept_id, semester))
                elif exam_type == "lab":
                    cur.execute("SELECT DISTINCT course_name, course_code, course_type FROM courses "
                                "WHERE dept_id=? AND semester=? AND course_type='lab' ORDER BY course_name",
                                (dept_id, semester))
                else:
                    cur.execute("SELECT DISTINCT course_name, course_code, course_type FROM courses "
                                "WHERE dept_id=? AND semester=? ORDER BY course_type DESC, course_name",
                                (dept_id, semester))

                courses = [dict(row) for row in cur.fetchall()]
                if courses:
                    exam_schedule = build_exam_schedule(courses, start_date, exams_per_day, selected["sessions"])
                else:
                    flash("No courses found for the selected filters.", "error")

        return render_template("exam_timetable.html",
                               departments=departments_list, exam_schedule=exam_schedule,
                               dept_name=dept_name, selected=selected, submitted=submitted)
    finally:
        conn.close()


@app.route("/exam-timetable/pdf")
def exam_timetable_pdf():
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    import io

    dept_id       = request.args.get("dept_id", "")
    semester      = request.args.get("semester", "")
    start_date    = request.args.get("start_date", "")
    exams_per_day = request.args.get("exams_per_day", "1")
    exam_type     = request.args.get("exam_type", "theory")
    sessions_raw  = request.args.get("sessions", "FN")
    sessions      = [s.strip() for s in sessions_raw.split(",") if s.strip()]

    conn = get_db()
    try:
        cur = conn.cursor()
        r = cur.execute("SELECT dept_name FROM departments WHERE dept_id=?", (dept_id,)).fetchone()
        dept_name = r["dept_name"] if r else "Department"

        if exam_type == "theory":
            cur.execute("SELECT DISTINCT course_name, course_code, course_type FROM courses "
                        "WHERE dept_id=? AND semester=? AND course_type='theory' ORDER BY course_name",
                        (dept_id, semester))
        elif exam_type == "lab":
            cur.execute("SELECT DISTINCT course_name, course_code, course_type FROM courses "
                        "WHERE dept_id=? AND semester=? AND course_type='lab' ORDER BY course_name",
                        (dept_id, semester))
        else:
            cur.execute("SELECT DISTINCT course_name, course_code, course_type FROM courses "
                        "WHERE dept_id=? AND semester=? ORDER BY course_type DESC, course_name",
                        (dept_id, semester))

        courses  = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

    schedule = build_exam_schedule(courses, start_date, exams_per_day, sessions)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)

    INK   = colors.HexColor("#1a1a2e"); MUTED = colors.HexColor("#6b7280")
    FN_BG = colors.HexColor("#fef9c3"); FN_FG = colors.HexColor("#854d0e")
    AN_BG = colors.HexColor("#dbeafe"); AN_FG = colors.HexColor("#1e40af")
    TH_BG = colors.HexColor("#eff6ff"); TH_FG = colors.HexColor("#1d4ed8")
    LB_BG = colors.HexColor("#f0fdf4"); LB_FG = colors.HexColor("#166534")
    ROW_A = colors.white; ROW_B = colors.HexColor("#f9faff"); GRID = colors.HexColor("#e5e7eb")

    title_st = ParagraphStyle("T", fontName="Helvetica-Bold", fontSize=16, textColor=INK, spaceAfter=4)
    sub_st   = ParagraphStyle("S", fontName="Helvetica", fontSize=9, textColor=MUTED, spaceAfter=16)

    story = [
        Paragraph("Exam Timetable", title_st),
        Paragraph(f"{dept_name}  |  Semester {semester}  |  "
                  f"Type: {exam_type.capitalize()}  |  Sessions: {', '.join(sessions)}", sub_st),
    ]

    header = ["#","Date","Day","Session","Course Name","Code","Type"]
    rows   = [header]
    for i, row in enumerate(schedule, 1):
        rows.append([str(i), row["date"], row["day"], row["session"],
                     row["course_name"], row["course_code"], row["course_type"].capitalize()])

    col_w = [8*mm,26*mm,13*mm,18*mm,76*mm,26*mm,20*mm]
    tbl   = Table(rows, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),INK), ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"), ("FONTSIZE",(0,0),(-1,-1),8),
        ("ALIGN",(0,0),(-1,0),"CENTER"),
        ("FONTNAME",(0,1),(-1,-1),"Helvetica"),
        ("TOPPADDING",(0,0),(-1,-1),6), ("BOTTOMPADDING",(0,0),(-1,-1),6),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[ROW_A,ROW_B]),
        ("GRID",(0,0),(-1,-1),0.4,GRID),
        ("ALIGN",(0,0),(3,-1),"CENTER"), ("ALIGN",(6,0),(6,-1),"CENTER"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ]))
    story.append(tbl)
    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"exam_timetable_sem{semester}.pdf")


# =========================================================
# SHARED VIEW ROUTES
# =========================================================
@app.route("/view/student/<int:dept_id>/<int:semester>")
def view_student_timetable(dept_id, semester):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT dept_name FROM departments WHERE dept_id=?", (dept_id,))
        dept      = cur.fetchone()
        dept_name = dept["dept_name"] if dept else "Unknown"

        cur.execute("SELECT * FROM timetable_settings LIMIT 1")
        settings = cur.fetchone()
        days, periods_per_day, period_times, break_times = [], 0, {}, {}
        if settings:
            periods_per_day = settings["periods_per_day"]
            working_days    = settings["working_days"] or "Mon,Tue,Wed,Thu,Fri,Sat"
            days            = [d.strip() for d in working_days.split(",")]
            start_time      = settings["start_time"] or "09:00"
            period_times, break_times = compute_period_times(
                start_time, settings["period_duration"], periods_per_day, settings["break_details"])

        cur.execute("""
            SELECT gt.day, gt.period,
                   c.course_name, c.course_code, c.course_type, c.elective_group_id,
                   f.faculty_name, f.faculty_type, eg.group_name
            FROM generated_timetable gt
            JOIN courses c   ON gt.course_id  = c.course_id
            JOIN faculties f ON gt.faculty_id = f.faculty_id
            LEFT JOIN elective_groups eg ON c.elective_group_id = eg.group_id
            WHERE gt.dept_id=? AND gt.semester=?
        """, (dept_id, semester))

        timetable = {}
        for row in cur.fetchall():
            key   = (row["day"], row["period"])
            entry = {
                "course_name": row["course_name"], "course_code": row["course_code"],
                "course_type": row["course_type"], "faculty_name": row["faculty_name"],
                "faculty_type": row["faculty_type"],
                "elective_group_id": row["elective_group_id"], "group_name": row["group_name"],
            }
            if key in timetable:
                if not isinstance(timetable[key], list):
                    timetable[key] = [timetable[key]]
                timetable[key].append(entry)
            else:
                timetable[key] = entry

        return render_template("student.html",
                               dept_name=dept_name, semester=semester,
                               timetable=timetable, days=days,
                               periods_per_day=periods_per_day,
                               period_times=period_times, break_times=break_times,
                               selected_dept=dept_id, selected_sem=semester,
                               departments=[], view_mode=True)
    finally:
        conn.close()


@app.route("/view/faculty/<int:faculty_id>")
def view_faculty_timetable(faculty_id):
    """Shared view — accepts optional ?semester_type=odd|even|all query param."""
    semester_type = request.args.get("semester_type", "all")
    if semester_type not in ("odd", "even", "all"):
        semester_type = "all"

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT faculty_name, faculty_type FROM faculties WHERE faculty_id=?", (faculty_id,))
        fac          = cur.fetchone()
        faculty_name = fac["faculty_name"] if fac else "Unknown"
        faculty_type = fac["faculty_type"] if fac else "regular"

        cur.execute("SELECT * FROM timetable_settings LIMIT 1")
        settings = cur.fetchone()
        days, periods_per_day, period_times, break_times = [], 0, {}, {}
        if settings:
            periods_per_day = settings["periods_per_day"]
            working_days    = settings["working_days"] or "Mon,Tue,Wed,Thu,Fri,Sat"
            days            = [d.strip() for d in working_days.split(",")]
            start_time      = settings["start_time"] or "09:00"
            period_times, break_times = compute_period_times(
                start_time, settings["period_duration"], periods_per_day, settings["break_details"])

        # Build semester parity filter
        if semester_type == "odd":
            sem_filter = "AND (gt.semester % 2 = 1)"
        elif semester_type == "even":
            sem_filter = "AND (gt.semester % 2 = 0)"
        else:
            sem_filter = ""

        cur.execute(f"""
            SELECT gt.day, gt.period, c.course_name, c.course_code, c.course_type,
                   d.dept_name, gt.semester
            FROM generated_timetable gt
            JOIN courses c     ON gt.course_id = c.course_id
            JOIN departments d ON gt.dept_id   = d.dept_id
            WHERE gt.faculty_id=? {sem_filter}
        """, (faculty_id,))

        timetable = {}
        for row in cur.fetchall():
            timetable[(row["day"], row["period"])] = {
                "course_name": row["course_name"], "course_code": row["course_code"],
                "course_type": row["course_type"], "dept_name": row["dept_name"],
                "semester": row["semester"],
            }
        return render_template("faculty_timetable.html",
                               faculty_name=faculty_name, faculty_type=faculty_type,
                               timetable=timetable, days=days,
                               periods_per_day=periods_per_day,
                               period_times=period_times, break_times=break_times,
                               selected_faculty_id=faculty_id,
                               selected_semester_type=semester_type,
                               departments=[], view_mode=True)
    finally:
        conn.close()


# =========================================================
# PDF ROUTES
# =========================================================
@app.route("/pdf/student")
def student_timetable_pdf():
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    import io

    dept_id  = request.args.get("dept_id", "")
    semester = request.args.get("semester", "")

    conn = get_db()
    try:
        cur = conn.cursor()
        r = cur.execute("SELECT dept_name FROM departments WHERE dept_id=?", (dept_id,)).fetchone()
        dept_name = r["dept_name"] if r else "Department"

        cur.execute("SELECT * FROM timetable_settings LIMIT 1")
        settings = cur.fetchone()
        days, periods_per_day, period_times, break_times = [], 0, {}, {}
        if settings:
            periods_per_day = settings["periods_per_day"]
            working_days    = settings["working_days"] or "Mon,Tue,Wed,Thu,Fri,Sat"
            days            = [d.strip() for d in working_days.split(",")]
            start_time      = settings["start_time"] or "09:00"
            period_times, break_times = compute_period_times(
                start_time, settings["period_duration"], periods_per_day, settings["break_details"])

        cur.execute("""
            SELECT gt.day, gt.period, c.course_name, c.course_code, c.course_type, f.faculty_name
            FROM generated_timetable gt
            JOIN courses c   ON gt.course_id  = c.course_id
            JOIN faculties f ON gt.faculty_id = f.faculty_id
            WHERE gt.dept_id=? AND gt.semester=?
        """, (dept_id, semester))

        timetable = {}
        for row in cur.fetchall():
            key = (row["day"], row["period"])
            if key not in timetable:
                timetable[key] = {"course_name": row["course_name"], "course_code": row["course_code"],
                                  "course_type": row["course_type"], "faculty_name": row["faculty_name"]}
            elif isinstance(timetable[key], list):
                timetable[key].append({"course_name": row["course_name"], "course_code": row["course_code"],
                                       "faculty_name": row["faculty_name"]})
            else:
                timetable[key] = [timetable[key],
                                  {"course_name": row["course_name"], "course_code": row["course_code"],
                                   "faculty_name": row["faculty_name"]}]
    finally:
        conn.close()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=12*mm, rightMargin=12*mm,
                            topMargin=12*mm, bottomMargin=12*mm)

    INK   = colors.HexColor("#1a1a2e"); MUTED = colors.HexColor("#6b7280")
    ROW_A = colors.white; ROW_B = colors.HexColor("#f9faff"); GRID = colors.HexColor("#e5e7eb")

    story = [
        Paragraph(f"Student Timetable — {dept_name}",
                  ParagraphStyle("T", fontName="Helvetica-Bold", fontSize=14, textColor=INK, spaceAfter=3)),
        Paragraph(f"Semester {semester}",
                  ParagraphStyle("S", fontName="Helvetica", fontSize=8, textColor=MUTED, spaceAfter=12)),
    ]

    header = ["Day"]
    for p in range(1, periods_per_day + 1):
        header.append(f"Period {p}\n{period_times.get(p,'')}")
        if p in break_times:
            header.append(f"Break\n{break_times[p]}")

    table_data = [header]
    for day in days:
        row_cells = [day]
        for p in range(1, periods_per_day + 1):
            cell = timetable.get((day, p))
            if cell is None:
                row_cells.append("—")
            elif isinstance(cell, list):
                text = " / ".join(f"{c['course_code']} ({c['faculty_name']})" for c in cell)
                row_cells.append(f"[ELECTIVE]\n{text}")
            else:
                row_cells.append(f"{cell['course_code']}\n{cell['course_name']}\n{cell['faculty_name']}")
            if p in break_times:
                row_cells.append("Break")
        table_data.append(row_cells)

    day_w    = 14*mm
    period_w = (270*mm - day_w - 14*mm * len(break_times)) / max(periods_per_day, 1)
    col_widths = [day_w]
    for p in range(1, periods_per_day + 1):
        col_widths.append(period_w)
        if p in break_times:
            col_widths.append(14*mm)

    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),INK), ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"), ("FONTSIZE",(0,0),(-1,-1),7),
        ("ALIGN",(0,0),(-1,-1),"CENTER"), ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),5), ("BOTTOMPADDING",(0,0),(-1,-1),5),
        ("BACKGROUND",(0,1),(0,-1),INK), ("TEXTCOLOR",(0,1),(0,-1),colors.white),
        ("FONTNAME",(0,1),(0,-1),"Helvetica-Bold"),
        ("ROWBACKGROUNDS",(1,1),(-1,-1),[ROW_A,ROW_B]),
        ("GRID",(0,0),(-1,-1),0.4,GRID), ("FONTNAME",(1,1),(-1,-1),"Helvetica"),
    ]))
    story.append(tbl)
    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"student_timetable_{dept_name}_sem{semester}.pdf")


@app.route("/pdf/faculty")
def faculty_timetable_pdf():
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    import io

    faculty_id    = request.args.get("faculty_id", "")
    semester_type = request.args.get("semester_type", "all")
    if semester_type not in ("odd", "even", "all"):
        semester_type = "all"

    conn = get_db()
    try:
        cur = conn.cursor()
        r = cur.execute("SELECT faculty_name FROM faculties WHERE faculty_id=?", (faculty_id,)).fetchone()
        faculty_name = r["faculty_name"] if r else "Faculty"

        cur.execute("SELECT * FROM timetable_settings LIMIT 1")
        settings = cur.fetchone()
        days, periods_per_day, period_times, break_times = [], 0, {}, {}
        if settings:
            periods_per_day = settings["periods_per_day"]
            working_days    = settings["working_days"] or "Mon,Tue,Wed,Thu,Fri,Sat"
            days            = [d.strip() for d in working_days.split(",")]
            start_time      = settings["start_time"] or "09:00"
            period_times, break_times = compute_period_times(
                start_time, settings["period_duration"], periods_per_day, settings["break_details"])

        # Build semester parity filter
        if semester_type == "odd":
            sem_filter = "AND (gt.semester % 2 = 1)"
        elif semester_type == "even":
            sem_filter = "AND (gt.semester % 2 = 0)"
        else:
            sem_filter = ""

        cur.execute(f"""
            SELECT gt.day, gt.period, c.course_name, c.course_code, c.course_type,
                   d.dept_name, gt.semester
            FROM generated_timetable gt
            JOIN courses c     ON gt.course_id = c.course_id
            JOIN departments d ON gt.dept_id   = d.dept_id
            WHERE gt.faculty_id=? {sem_filter}
        """, (faculty_id,))

        timetable = {}
        for row in cur.fetchall():
            timetable[(row["day"], row["period"])] = {
                "course_name": row["course_name"], "course_code": row["course_code"],
                "course_type": row["course_type"], "dept_name": row["dept_name"],
                "semester": row["semester"],
            }
    finally:
        conn.close()

    # Build subtitle
    sem_label = {"odd": " — Odd Semesters", "even": " — Even Semesters"}.get(semester_type, "")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=12*mm, rightMargin=12*mm,
                            topMargin=12*mm, bottomMargin=12*mm)

    INK   = colors.HexColor("#1a1a2e"); MUTED = colors.HexColor("#6b7280")
    ROW_A = colors.white; ROW_B = colors.HexColor("#f9faff"); GRID = colors.HexColor("#e5e7eb")

    story = [
        Paragraph(f"Faculty Timetable — {faculty_name}",
                  ParagraphStyle("T", fontName="Helvetica-Bold", fontSize=14, textColor=INK, spaceAfter=3)),
        Paragraph(f"Weekly Schedule{sem_label}",
                  ParagraphStyle("S", fontName="Helvetica", fontSize=8, textColor=MUTED, spaceAfter=12)),
    ]

    header = ["Day"]
    for p in range(1, periods_per_day + 1):
        header.append(f"Period {p}\n{period_times.get(p,'')}")
        if p in break_times:
            header.append(f"Break\n{break_times[p]}")

    table_data = [header]
    for day in days:
        row_cells = [day]
        for p in range(1, periods_per_day + 1):
            cell = timetable.get((day, p))
            row_cells.append(
                f"{cell['course_code']}\n{cell['course_name']}\n{cell['dept_name']} Sem {cell['semester']}"
                if cell else "—"
            )
            if p in break_times:
                row_cells.append("Break")
        table_data.append(row_cells)

    day_w    = 14*mm
    period_w = (270*mm - day_w - 14*mm * len(break_times)) / max(periods_per_day, 1)
    col_widths = [day_w]
    for p in range(1, periods_per_day + 1):
        col_widths.append(period_w)
        if p in break_times:
            col_widths.append(14*mm)

    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),INK), ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"), ("FONTSIZE",(0,0),(-1,-1),7),
        ("ALIGN",(0,0),(-1,-1),"CENTER"), ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),5), ("BOTTOMPADDING",(0,0),(-1,-1),5),
        ("BACKGROUND",(0,1),(0,-1),INK), ("TEXTCOLOR",(0,1),(0,-1),colors.white),
        ("FONTNAME",(0,1),(0,-1),"Helvetica-Bold"),
        ("ROWBACKGROUNDS",(1,1),(-1,-1),[ROW_A,ROW_B]),
        ("GRID",(0,0),(-1,-1),0.4,GRID),
    ]))
    story.append(tbl)
    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"faculty_timetable_{faculty_name}.pdf")


# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)