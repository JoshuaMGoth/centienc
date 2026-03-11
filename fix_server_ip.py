import sqlite3
db = sqlite3.connect("/var/lib/centient/centient.db")
db.row_factory = sqlite3.Row
rows = db.execute("SELECT id, name, hostname, ip_address FROM servers").fetchall()
print("Current servers:")
for r in rows:
    print(f"  id={r[0]} name={r[1]} hostname={r[2]} ip={r[3]}")
db.execute("UPDATE servers SET hostname='100.85.118.113', ip_address='100.85.118.113' WHERE hostname='10.10.10.55' OR ip_address='10.10.10.55'")
db.execute("UPDATE servers SET hostname='100.85.118.113', ip_address='100.85.118.113' WHERE hostname='10.10.10.127' OR ip_address='10.10.10.127'")
db.commit()
rows = db.execute("SELECT id, name, hostname, ip_address FROM servers").fetchall()
print("After update:")
for r in rows:
    print(f"  id={r[0]} name={r[1]} hostname={r[2]} ip={r[3]}")
db.close()
print("Done")
