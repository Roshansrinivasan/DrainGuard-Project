"""Create (or with --reset, wipe and re-create) the SQLite database with demo data."""
import sys
import db
db.init_db(reset="--reset" in sys.argv)
print("Database ready." + (" (reset)" if "--reset" in sys.argv else ""))
