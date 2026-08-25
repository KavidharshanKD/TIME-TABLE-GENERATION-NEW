# 📅 Automated Academic Timetable & Faculty Allocation System

A comprehensive, Flask-powered web application designed for educational institutions to manage departments, courses, faculty allocations, automated weekly class timetable generation, exam schedule planning, and instant PDF report exports.

---

## 🌟 Key Features

- 🏢 **Department & Course Management**: Organize academic departments, theory courses, lab courses, and elective group mappings.
- 👨‍🏫 **Faculty Allocation Survey Engine**: Generate secure token-based preference forms, set allocation quotas, and manage allocation rounds.
- 📧 **Built-in SMTP Integration**: Configure custom email servers (including Google App Passwords) to auto-distribute allocation survey links to faculty.
- ⚡ **Automated Class Timetable Generation**: Intelligent algorithm to generate conflict-free weekly timetables for all departments and semesters.
- 📝 **Exam Timetable Generator**: Easily build theory and lab examination schedules with customizable dates and FN/AN session slots.
- 📄 **Instant PDF Exports**: Generate and download publication-ready PDF documents for:
  - 🎓 Student Timetables (Department & Semester-wise)
  - 👨‍🏫 Faculty Timetables (Individual teacher schedules)
  - 📅 Exam Schedules (Theory & Practical examinations)
- 🎨 **Modern Responsive UI**: Built with HTML5, Vanilla CSS glassmorphism styling, and dynamic typography (`DM Sans` & `DM Serif Display`).
- 🗄️ **Dual Database Architecture**: Primary lightweight SQLite engine with automatic schema migrations + optional MySQL connectivity support.

---

## 📦 Dependencies

### 1. External Python Packages (Installed via `pip`)

| Package | Minimum Version | Purpose |
| :--- | :--- | :--- |
| **`flask`** | `>= 3.0.0` | Core Web Framework for routing, HTML rendering, REST API, flash messages, and file handling |
| **`reportlab`** | `>= 4.0.0` | Dynamic PDF generation engine for student, faculty, and exam timetables |
| **`pymysql`** | `>= 1.1.0` | Pure-Python MySQL database driver for MySQL connection management |
| **`mysql-connector-python`** | `>= 8.2.0` | Official MySQL driver used for MySQL database testing |

### 2. Python Standard Library Modules (Pre-installed with Python)

- **`sqlite3`**: Embedded database storage engine (`timetable.db` with WAL mode & automated schema migration).
- **`random`**: Randomization algorithms for preference allocation and timetable scheduling.
- **`re`**: Pattern matching and regular expressions for email and token validation.
- **`secrets`**: Cryptographically secure token generation for allocation survey URLs.
- **`json`**: JSON data parsing for API requests and quota metadata.
- **`os`**: Operating system utilities and filepath management.
- **`smtplib`**: SMTP connection engine for sending allocation emails.
- **`email.mime`** (`MIMEMultipart`, `MIMEText`): Construction of HTML and plain-text emails.
- **`datetime`** (`datetime`, `date`, `timedelta`): Date calculations for exam timetables and timestamps.
- **`io`** (`BytesIO`): In-memory binary buffer for streaming PDF downloads without writing temp files to disk.

### 3. Frontend Assets & CDN Dependencies

- **Google Fonts CDN**: `DM Sans` (Sans-serif typography) & `DM Serif Display` (Serif header typography).
- **Vanilla CSS**: Custom styling with glassmorphism effects, flexbox/grid layouts, and responsive CSS variables.

---

## 🚀 Quick Start & Installation

### Prerequisites

- **Python**: Version `3.10` or higher installed on your system.

### Installation Steps

1. **Clone or Download the Repository**
   ```bash
   git clone <repository-url>
   cd TIME-TABLE-GENERATION-NEW
   ```

2. **Create and Activate a Virtual Environment**
   - **Windows (PowerShell):**
     ```powershell
     python -m venv venv
     .\venv\Scripts\Activate.ps1
     ```
   - **Linux / macOS:**
     ```bash
     python3 -m venv venv
     source venv/bin/activate
     ```

3. **Install Required Packages**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the Application**
   ```bash
   python app.py
   ```

5. **Access the Web Portal**
   Open your browser and navigate to:
   ```text
   http://127.0.0.1:5000
   ```

---

## 📂 Project Architecture

```text
TIME-TABLE-GENERATION-NEW/
│
├── app.py                      # Main Flask Application (Routes, Logic, PDF Engine, Migrations)
├── database.py                 # MySQL / SQLite helper connection functions
├── db_test.py                  # Utility script to test MySQL database connection
├── timetable.db                # SQLite database (Auto-created on launch)
├── requirements.txt            # Python dependencies specification file
├── README.md                   # Project documentation
│
├── static/                     # Static Web Assets
│   ├── style.css               # Main application stylesheet (Glassmorphism & Layouts)
│   ├── images/                 # Background images & visual assets
│   └── videos/                 # Background video elements
│
└── templates/                  # Jinja2 HTML Templates
    ├── base.html               # Base layout template
    ├── index.html / home.html  # Landing page
    ├── choose_role.html        # Role selection portal (Admin, Faculty, Student)
    ├── departments.html        # Department management view
    ├── courses.html            # Course management view
    ├── faculties.html          # Faculty member management view
    ├── elective_groups.html    # Elective course mapping view
    ├── allocation_setup.html   # Faculty allocation setup page
    ├── allocation_form.html    # Faculty preference submission form
    ├── generate_timetable.html # Timetable generator dashboard
    ├── exam_timetable.html     # Exam timetable scheduler
    ├── student.html            # Student timetable viewer
    ├── faculty_home.html       # Faculty timetable viewer
    └── details.html            # System details & SMTP configuration page
```

---

## 🗺️ Key Application Routes

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/` | `GET` | Home / Landing Page |
| `/choose-role` | `GET` | Role selection portal |
| `/student` | `GET`, `POST` | Student timetable search & view |
| `/faculty` | `GET`, `POST` | Faculty timetable search & view |
| `/departments` | `GET`, `POST` | View and add departments |
| `/courses/<dept_id>` | `GET`, `POST` | Manage courses for a specific department |
| `/faculties/<dept_id>` | `GET`, `POST` | Manage faculty members for a specific department |
| `/allocation/setup` | `GET`, `POST` | Configure faculty allocation survey rounds |
| `/allocation/form/<token>` | `GET`, `POST` | Faculty survey preference submission portal |
| `/generate-timetable` | `GET`, `POST` | Generate automated weekly class timetable |
| `/exam-timetable` | `GET`, `POST` | Generate theory/lab examination schedule |
| `/pdf/student` | `GET` | Download Student Timetable in PDF format |
| `/pdf/faculty` | `GET` | Download Faculty Timetable in PDF format |
| `/exam-timetable/pdf` | `GET` | Download Exam Schedule in PDF format |

---

## 🛠️ Database Configuration

### SQLite (Default)
The application automatically creates and manages `timetable.db` using SQLite with `WAL` (Write-Ahead Logging) mode and automatic migration hooks upon launching `app.py`.

### MySQL (Optional Setup)
If connecting to a MySQL database instance, update credentials in `database.py`:
```python
def get_connection():
    return pymysql.connect(
        host="localhost",
        user="root",
        password="your_password",
        database="timetable_db"
    )
```
Test the MySQL database connection by running:
```bash
python db_test.py
```

---

## 📜 License

This project is developed for academic timetable management and allocation systems. Feel free to customize and extend it for institutional use.
