import pymysql
import sqlite3
def get_connection():
    return pymysql.connect(
        host="localhost",
        user="root",
        password="root123",
        database="timetable_db"
    )


def create_tables():
    conn = sqlite3.connect("timetable.db")
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS department(
        dept_id INTEGER PRIMARY KEY AUTOINCREMENT,
        dept_name TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()
