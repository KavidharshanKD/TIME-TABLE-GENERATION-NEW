import mysql.connector

# connect to mysql
mydb = mysql.connector.connect(
    host="localhost",
    user="root",
    password="root123",
    database="timetable_db"
)

print("Connected successfully!")

mycursor = mydb.cursor()

# check admin table
mycursor.execute("SELECT * FROM admin")

for x in mycursor:
    print(x)
