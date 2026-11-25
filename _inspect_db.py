import sqlite3

c = sqlite3.connect(r"backend\netracare.db")
for r in c.execute(
    "select id,speciality,date,start_time,status from appointments "
    "where patient_id='P01' order by id"
):
    print(r)
