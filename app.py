from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
import sqlite3
import random
import re

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production-12345'

# =========================================================
# DATABASE CONNECTION
# =========================================================
def get_db():
    conn = sqlite3.connect("timetable.db")
    conn.row_factory = sqlite3.Row
    return conn


# =========================================================
# CREATE TABLES
# =========================================================
def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS departments(
        dept_id INTEGER PRIMARY KEY,
        dept_name TEXT NOT NULL
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS faculties(
        faculty_id INTEGER PRIMARY KEY AUTOINCREMENT,
        faculty_name TEXT NOT NULL,
        dept_id INTEGER,
        FOREIGN KEY(dept_id) REFERENCES departments(dept_id)
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS courses(
        course_id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_name TEXT,
        course_code TEXT,
        semester INTEGER,
        credits INTEGER,
        faculty_id INTEGER,
        dept_id INTEGER,
        course_type TEXT NOT NULL DEFAULT 'theory',
        FOREIGN KEY(faculty_id) REFERENCES faculties(faculty_id),
        FOREIGN KEY(dept_id) REFERENCES departments(dept_id)
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS timetable_settings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        periods_per_day INTEGER,
        period_duration INTEGER,
        number_of_breaks INTEGER,
        break_details TEXT,
        working_days TEXT DEFAULT 'Mon,Tue,Wed,Thu,Fri,Sat',
        start_time TEXT DEFAULT '09:00'
    )""")

    for col, default in [("working_days", "'Mon,Tue,Wed,Thu,Fri,Sat'"), ("start_time", "'09:00'")]:
        try:
            cur.execute(f"ALTER TABLE timetable_settings ADD COLUMN {col} TEXT DEFAULT {default}")
        except:
            pass

    cur.execute("""
    CREATE TABLE IF NOT EXISTS generated_timetable(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dept_id INTEGER,
        semester INTEGER,
        day TEXT,
        period INTEGER,
        course_id INTEGER,
        faculty_id INTEGER,
        FOREIGN KEY(dept_id) REFERENCES departments(dept_id),
        FOREIGN KEY(course_id) REFERENCES courses(course_id),
        FOREIGN KEY(faculty_id) REFERENCES faculties(faculty_id)
    )""")

    conn.commit()
    conn.close()

init_db()


# =========================================================
# HELPERS
# =========================================================
def compute_period_times(start_time_str, period_duration, periods_per_day, break_details_str):
    """
    Returns {period_number: "HH:MM - HH:MM"} and {after_period: label} for break rows.
    break_details_str is JSON: [{"after_period": 2, "duration": 15}, ...]
    """
    import json
    breaks = {}  # {after_period: duration_minutes}
    if break_details_str:
        try:
            parsed = json.loads(break_details_str)
            for b in parsed:
                breaks[int(b["after_period"])] = int(b["duration"])
        except:
            pass

    try:
        h, m = map(int, start_time_str.split(":"))
        current = h * 60 + m
    except:
        current = 9 * 60

    def fmt(mins):
        return f"{mins // 60:02d}:{mins % 60:02d}"

    period_times = {}
    break_times = {}  # {after_period: "HH:MM - HH:MM"}

    for p in range(1, periods_per_day + 1):
        period_end = current + period_duration
        period_times[p] = f"{fmt(current)} - {fmt(period_end)}"
        current = period_end
        # Apply break after this period if one exists
        if p in breaks:
            break_end = current + breaks[p]
            break_times[p] = f"{fmt(current)} - {fmt(break_end)}"
            current = break_end

    return period_times, break_times


# =========================================================
# GLOBAL NAVBAR CONTEXT
# =========================================================
@app.context_processor
def inject_departments():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM departments")
    depts = cur.fetchall()
    conn.close()
    return dict(nav_departments=depts)


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


# =========================================================
# CHOOSE ROLE
# =========================================================
@app.route("/choose-role")
def choose_role():
    return render_template("choose_role.html")


# =========================================================
# API: CHECK SETUP
# =========================================================
@app.route("/api/check-setup")
def api_check_setup():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM departments")
    dept_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM faculties")
    fac_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM courses")
    course_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM generated_timetable")
    tt_count = cur.fetchone()[0]
    conn.close()
    ready = dept_count > 0 and fac_count > 0 and course_count > 0 and tt_count > 0
    return jsonify({"ready": ready})


# =========================================================
# STUDENT TIMETABLE
# =========================================================
@app.route("/student", methods=["GET", "POST"])
def student():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM departments")
    departments = cur.fetchall()

    timetable = None
    selected_dept = None
    selected_sem = None
    days = []
    periods_per_day = 0
    period_times = {}

    if request.method == "POST":
        dept_id = request.form.get("dept_id")
        semester = request.form.get("semester")
        selected_dept = dept_id
        selected_sem = semester

        cur.execute("SELECT * FROM timetable_settings LIMIT 1")
        settings = cur.fetchone()

        if settings:
            periods_per_day = settings["periods_per_day"]
            working_days = settings["working_days"] if settings["working_days"] else "Mon,Tue,Wed,Thu,Fri,Sat"
            days = [d.strip() for d in working_days.split(",")]
            start_time = settings["start_time"] if settings["start_time"] else "09:00"
            period_times, break_times = compute_period_times(start_time, settings["period_duration"], periods_per_day, settings["break_details"])

        cur.execute("""
            SELECT gt.day, gt.period, c.course_name, c.course_code, c.course_type, f.faculty_name
            FROM generated_timetable gt
            JOIN courses c ON gt.course_id = c.course_id
            JOIN faculties f ON gt.faculty_id = f.faculty_id
            WHERE gt.dept_id=? AND gt.semester=?
        """, (dept_id, semester))

        rows = cur.fetchall()
        timetable = {}
        for row in rows:
            key = (row["day"], row["period"])
            timetable[key] = {
                "course_name": row["course_name"],
                "course_code": row["course_code"],
                "course_type": row["course_type"],
                "faculty_name": row["faculty_name"]
            }

    conn.close()
    return render_template("student.html", departments=departments, timetable=timetable,
                           days=days, periods_per_day=periods_per_day, selected_dept=selected_dept,
                           selected_sem=selected_sem, period_times=period_times,
                           break_times=break_times if 'break_times' in locals() else {})


# =========================================================
# FACULTY TIMETABLE
# =========================================================
@app.route("/faculty", methods=["GET", "POST"])
def faculty():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM departments")
    departments = cur.fetchall()

    timetable = None
    days = []
    periods_per_day = 0
    selected_faculty_id = None
    period_times = {}

    if request.method == "POST":
        faculty_id = request.form.get("faculty_id")
        selected_faculty_id = faculty_id

        cur.execute("SELECT * FROM timetable_settings LIMIT 1")
        settings = cur.fetchone()

        if settings:
            periods_per_day = settings["periods_per_day"]
            working_days = settings["working_days"] if settings["working_days"] else "Mon,Tue,Wed,Thu,Fri,Sat"
            days = [d.strip() for d in working_days.split(",")]
            start_time = settings["start_time"] if settings["start_time"] else "09:00"
            period_times, break_times = compute_period_times(start_time, settings["period_duration"], periods_per_day, settings["break_details"])

        cur.execute("""
            SELECT gt.day, gt.period, c.course_name, c.course_code, c.course_type, d.dept_name, gt.semester
            FROM generated_timetable gt
            JOIN courses c ON gt.course_id = c.course_id
            JOIN departments d ON gt.dept_id = d.dept_id
            WHERE gt.faculty_id=?
        """, (faculty_id,))

        rows = cur.fetchall()
        timetable = {}
        for row in rows:
            key = (row["day"], row["period"])
            timetable[key] = {
                "course_name": row["course_name"],
                "course_code": row["course_code"],
                "course_type": row["course_type"],
                "dept_name": row["dept_name"],
                "semester": row["semester"]
            }

    conn.close()
    return render_template("faculty_timetable.html", departments=departments, timetable=timetable,
                           days=days, periods_per_day=periods_per_day,
                           selected_faculty_id=selected_faculty_id, period_times=period_times,
                           break_times=break_times if 'break_times' in dir() else {})


# =========================================================
# DETAILS PAGE
# =========================================================
@app.route("/details", methods=["GET", "POST"])
def details():
    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        periods_per_day = request.form.get("periods_per_day")
        period_duration = request.form.get("period_duration")
        number_of_breaks = request.form.get("number_of_breaks")
        working_days_list = request.form.getlist("working_days")
        working_days = ",".join(working_days_list)
        start_time = request.form.get("start_time", "09:00")

        # Parse per-break inputs into JSON
        import json
        after_periods = request.form.getlist("break_after_period")
        durations = request.form.getlist("break_duration")
        breaks_data = []
        for ap, dur in zip(after_periods, durations):
            if ap and dur:
                breaks_data.append({"after_period": int(ap), "duration": int(dur)})
        break_details = json.dumps(breaks_data)

        cur.execute("DELETE FROM timetable_settings")
        cur.execute("""
            INSERT INTO timetable_settings
            (periods_per_day, period_duration, number_of_breaks, break_details, working_days, start_time)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (periods_per_day, period_duration, number_of_breaks, break_details, working_days, start_time))
        conn.commit()
        flash("Settings saved successfully!", "success")

    cur.execute("SELECT * FROM timetable_settings LIMIT 1")
    settings = cur.fetchone()

    cur.execute("SELECT COUNT(*) FROM departments")
    total_departments = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM faculties")
    total_faculties = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM courses")
    total_courses = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM generated_timetable")
    total_timetable_entries = cur.fetchone()[0]

    conn.close()

    saved_days = []
    if settings and settings["working_days"]:
        saved_days = [d.strip() for d in settings["working_days"].split(",")]

    import json
    saved_breaks = []
    if settings and settings["break_details"]:
        try:
            saved_breaks = json.loads(settings["break_details"])
        except:
            saved_breaks = []

    all_days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    return render_template("details.html", settings=settings,
                           total_departments=total_departments, total_faculties=total_faculties,
                           total_courses=total_courses, total_timetable_entries=total_timetable_entries,
                           all_days=all_days, saved_days=saved_days, saved_breaks=saved_breaks)


# =========================================================
# DEPARTMENTS
# =========================================================
@app.route("/departments", methods=["GET", "POST"])
def departments():
    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        dept_id = request.form.get("dept_id")
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
    departments = cur.fetchall()
    conn.close()
    return render_template("departments.html", departments=departments)


@app.route("/delete_department/<int:dept_id>")
def delete_department(dept_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM courses WHERE dept_id=?", (dept_id,))
    cur.execute("DELETE FROM faculties WHERE dept_id=?", (dept_id,))
    cur.execute("DELETE FROM generated_timetable WHERE dept_id=?", (dept_id,))
    cur.execute("DELETE FROM departments WHERE dept_id=?", (dept_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("departments"))


# =========================================================
# FACULTY MANAGEMENT
# =========================================================
@app.route("/faculty_home")
def faculty_home():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM departments")
    departments = cur.fetchall()
    conn.close()
    return render_template("faculty_home.html", departments=departments)


@app.route("/faculties/<int:dept_id>", methods=["GET", "POST"])
def faculties(dept_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM departments WHERE dept_id=?", (dept_id,))
    dept = cur.fetchone()
    if not dept:
        conn.close()
        return "Department not found"

    if request.method == "POST":
        faculty_name = request.form.get("faculty_name")
        if faculty_name:
            cur.execute("INSERT INTO faculties (faculty_name, dept_id) VALUES (?,?)", (faculty_name, dept_id))
            conn.commit()

    cur.execute("SELECT * FROM faculties WHERE dept_id=?", (dept_id,))
    faculties = cur.fetchall()
    conn.close()
    return render_template("faculties.html", faculties=faculties, dept_id=dept_id, dept_name=dept["dept_name"])


@app.route("/delete_faculty/<int:dept_id>/<int:faculty_id>")
def delete_faculty(dept_id, faculty_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM courses WHERE faculty_id=?", (faculty_id,))
    cur.execute("DELETE FROM faculties WHERE faculty_id=?", (faculty_id,))
    cur.execute("SELECT COUNT(*) FROM faculties")
    if cur.fetchone()[0] == 0:
        cur.execute("DELETE FROM sqlite_sequence WHERE name='faculties'")
    conn.commit()
    conn.close()
    return redirect(url_for("faculties", dept_id=dept_id))


# =========================================================
# COURSES
# =========================================================
@app.route("/courses")
def courses_home():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM departments")
    departments = cur.fetchall()
    conn.close()
    return render_template("courses_home.html", departments=departments)


@app.route("/courses/<int:dept_id>", methods=["GET", "POST"])
def courses(dept_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM departments WHERE dept_id=?", (dept_id,))
    dept = cur.fetchone()
    if not dept:
        conn.close()
        return "Department not found"

    if request.method == "POST":
        course_name = request.form.get("course_name")
        course_code = request.form.get("course_code")
        semester = request.form.get("semester")
        credits = request.form.get("credits")
        faculty_id = request.form.get("faculty_id")
        course_type = request.form.get("course_type")

        if all([course_name, course_code, semester, credits, faculty_id, course_type]):
            cur.execute("""
                INSERT INTO courses (course_name, course_code, semester, credits, faculty_id, dept_id, course_type)
                VALUES (?,?,?,?,?,?,?)
            """, (course_name, course_code, semester, credits, faculty_id, dept_id, course_type))
            conn.commit()
        return redirect(url_for("courses", dept_id=dept_id))

    cur.execute("""
        SELECT c.course_id, c.course_name, c.course_code, c.semester, c.credits, c.course_type, f.faculty_name
        FROM courses c
        LEFT JOIN faculties f ON c.faculty_id = f.faculty_id
        WHERE c.dept_id=? ORDER BY c.semester ASC
    """, (dept_id,))
    courses = cur.fetchall()
    conn.close()
    return render_template("courses.html", courses=courses, dept_id=dept_id, dept_name=dept["dept_name"])


@app.route("/delete_course/<int:course_id>/<int:dept_id>")
def delete_course(course_id, dept_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM courses WHERE course_id=?", (course_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("courses", dept_id=dept_id))


# =========================================================
# API - FACULTIES BY DEPARTMENT
# =========================================================
@app.route("/api/faculties/<int:dept_id>")
def api_faculties(dept_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT faculty_id, faculty_name FROM faculties WHERE dept_id=?", (dept_id,))
    faculties = cur.fetchall()
    conn.close()
    return jsonify([{"faculty_id": f["faculty_id"], "faculty_name": f["faculty_name"]} for f in faculties])


# =========================================================
# TIMETABLE GENERATION LOGIC
# =========================================================
def generate_timetable_logic(selected_dept_ids):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM timetable_settings LIMIT 1")
    settings = cur.fetchone()

    if not settings:
        conn.close()
        return False, "No timetable settings found. Please configure settings first."

    periods_per_day = settings["periods_per_day"]
    working_days_str = settings["working_days"] if settings["working_days"] else "Mon,Tue,Wed,Thu,Fri,Sat"
    days = [d.strip() for d in working_days_str.split(",")]

    # Delete only selected depts
    for dept_id in selected_dept_ids:
        cur.execute("DELETE FROM generated_timetable WHERE dept_id=?", (dept_id,))

    # Load existing faculty busy slots (other depts not being regenerated)
    faculty_busy = {day: {p: set() for p in range(1, periods_per_day + 1)} for day in days}
    cur.execute("SELECT day, period, faculty_id FROM generated_timetable")
    for row in cur.fetchall():
        if row["day"] in faculty_busy and row["period"] in faculty_busy[row["day"]]:
            faculty_busy[row["day"]][row["period"]].add(row["faculty_id"])

    entries_to_insert = []

    for dept_id in selected_dept_ids:
        cur.execute("SELECT DISTINCT semester FROM courses WHERE dept_id=?", (dept_id,))
        semesters = [row["semester"] for row in cur.fetchall()]

        for semester in semesters:
            # dept+sem slot tracker
            slot_taken = {day: {p: False for p in range(1, periods_per_day + 1)} for day in days}

            cur.execute("""
                SELECT course_id, credits, faculty_id, course_type
                FROM courses WHERE dept_id=? AND semester=?
            """, (dept_id, semester))
            courses_list = list(cur.fetchall())

            # Build assignments
            assignments = []
            for course in courses_list:
                if course["course_type"] == "lab":
                    assignments.append({
                        "course_id": course["course_id"],
                        "faculty_id": course["faculty_id"],
                        "course_type": "lab"
                    })
                else:
                    slots = course["credits"] if course["credits"] else 3
                    for _ in range(slots):
                        assignments.append({
                            "course_id": course["course_id"],
                            "faculty_id": course["faculty_id"],
                            "course_type": "theory"
                        })

            random.shuffle(assignments)

            # Parse break positions so we can exclude slots that straddle a break
            import json
            break_after_periods = set()
            if settings["break_details"]:
                try:
                    for b in json.loads(settings["break_details"]):
                        break_after_periods.add(int(b["after_period"]))
                except:
                    pass

            # Slot pools — period-first so courses spread across all days at morning periods
            # Period 1 fills across all days first, then period 2, etc. Free slots fall at end of every day
            theory_slots = [(day, p) for p in range(1, periods_per_day + 1) for day in days]
            # Lab slots: exclude any pair (p, p+1) where a break falls between them
            lab_slots = [
                (day, p)
                for p in range(1, periods_per_day)
                for day in days
                if p not in break_after_periods  # skip if break falls between p and p+1
            ]

            for assignment in assignments:
                faculty_id = assignment["faculty_id"]
                is_lab = assignment["course_type"] == "lab"
                pool = lab_slots if is_lab else theory_slots

                for (day, p) in pool:
                    if is_lab:
                        p2 = p + 1
                        if (not slot_taken[day][p] and not slot_taken[day][p2] and
                                faculty_id not in faculty_busy[day][p] and
                                faculty_id not in faculty_busy[day][p2]):
                            slot_taken[day][p] = True
                            slot_taken[day][p2] = True
                            faculty_busy[day][p].add(faculty_id)
                            faculty_busy[day][p2].add(faculty_id)
                            entries_to_insert.append((dept_id, semester, day, p, assignment["course_id"], faculty_id))
                            entries_to_insert.append((dept_id, semester, day, p2, assignment["course_id"], faculty_id))
                            break
                    else:
                        if (not slot_taken[day][p] and faculty_id not in faculty_busy[day][p]):
                            # Check no same course in adjacent periods (p-1 or p+1) on same day
                            prev_ok = True
                            next_ok = True
                            for (d2, p2, cid2, _) in [(e[2], e[3], e[4], None) for e in entries_to_insert
                                                       if e[0] == dept_id and e[1] == semester and e[2] == day]:
                                if cid2 == assignment["course_id"]:
                                    if p2 == p - 1 or p2 == p + 1:
                                        prev_ok = False
                                        break
                            if not prev_ok:
                                continue  # Try next slot to avoid consecutive same course
                            slot_taken[day][p] = True
                            faculty_busy[day][p].add(faculty_id)
                            entries_to_insert.append((dept_id, semester, day, p, assignment["course_id"], faculty_id))
                            break

    cur.executemany("""
        INSERT INTO generated_timetable (dept_id, semester, day, period, course_id, faculty_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, entries_to_insert)

    conn.commit()
    conn.close()
    return True, f"Timetable generated for {len(selected_dept_ids)} department(s). {len(entries_to_insert)} slots assigned."


@app.route("/generate-timetable", methods=["GET", "POST"])
def generate_timetable():
    conn = get_db()
    cur = conn.cursor()

    result_message = None
    result_type = None
    preview = {}
    selected_dept_ids = []

    cur.execute("SELECT * FROM timetable_settings LIMIT 1")
    settings = cur.fetchone()

    days = []
    periods_per_day = 0
    period_times = {}
    break_times = {}

    if settings:
        periods_per_day = settings["periods_per_day"]
        working_days_str = settings["working_days"] if settings["working_days"] else "Mon,Tue,Wed,Thu,Fri,Sat"
        days = [d.strip() for d in working_days_str.split(",")]
        start_time = settings["start_time"] if settings["start_time"] else "09:00"
        period_times, break_times = compute_period_times(start_time, settings["period_duration"], periods_per_day, settings["break_details"])

    if request.method == "POST":
        selected_dept_ids = [int(x) for x in request.form.getlist("selected_depts")]
        if not selected_dept_ids:
            result_message = "Please select at least one department."
            result_type = "error"
        else:
            success, message = generate_timetable_logic(selected_dept_ids)
            result_message = message
            result_type = "success" if success else "error"

    cur.execute("SELECT * FROM departments")
    all_departments = cur.fetchall()

    for dept in all_departments:
        dept_id = dept["dept_id"]
        preview[dept_id] = {"dept_name": dept["dept_name"], "semesters": {}}

        cur.execute("SELECT DISTINCT semester FROM generated_timetable WHERE dept_id=? ORDER BY semester", (dept_id,))
        semesters = [row["semester"] for row in cur.fetchall()]

        for sem in semesters:
            grid = {day: {p: None for p in range(1, periods_per_day + 1)} for day in days}

            cur.execute("""
                SELECT gt.day, gt.period, c.course_name, c.course_code, c.course_type, f.faculty_name
                FROM generated_timetable gt
                JOIN courses c ON gt.course_id = c.course_id
                JOIN faculties f ON gt.faculty_id = f.faculty_id
                WHERE gt.dept_id=? AND gt.semester=?
            """, (dept_id, sem))

            for row in cur.fetchall():
                if row["day"] in grid:
                    grid[row["day"]][row["period"]] = {
                        "course_name": row["course_name"],
                        "course_code": row["course_code"],
                        "course_type": row["course_type"],
                        "faculty_name": row["faculty_name"]
                    }

            preview[dept_id]["semesters"][sem] = grid

    conn.close()

    return render_template("generate_timetable.html",
                           result_message=result_message, result_type=result_type,
                           preview=preview, days=days, periods_per_day=periods_per_day,
                           settings=settings, all_departments=all_departments,
                           selected_dept_ids=selected_dept_ids, period_times=period_times,
                           break_times=break_times)


# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    app.run(debug=True)