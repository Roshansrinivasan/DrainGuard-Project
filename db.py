"""SQLite helpers: connection, schema, seed (demo) data."""
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta

from flask import g
from werkzeug.security import generate_password_hash

import config


def ts(dt=None):
    """Timestamp string used everywhere in the DB (local time)."""
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'admin',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  drain_code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  location TEXT NOT NULL,
  latitude REAL NOT NULL,
  longitude REAL NOT NULL,
  water_level REAL NOT NULL DEFAULT 0,
  waste_level REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'normal',
  is_demo INTEGER NOT NULL DEFAULT 0,
  verified INTEGER NOT NULL DEFAULT 0,
  source TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS citizen_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT UNIQUE NOT NULL,
  drain_id INTEGER REFERENCES drains(id),
  reporter_name TEXT,
  location_text TEXT,
  latitude REAL,
  longitude REAL,
  description TEXT NOT NULL,
  waste_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploaded_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT,
  filename TEXT UNIQUE NOT NULL,
  original_name TEXT,
  size_bytes INTEGER,
  ai_mode TEXT,
  ai_categories TEXT,
  ai_severity TEXT,
  ai_confidence REAL,
  ai_action TEXT,
  uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hardware_devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  device_type TEXT NOT NULL DEFAULT 'real',      -- 'real' or 'simulated'
  drain_id INTEGER REFERENCES drains(id),
  api_key TEXT,
  water_level REAL NOT NULL DEFAULT 0,
  waste_level REAL NOT NULL DEFAULT 0,
  basket_level REAL NOT NULL DEFAULT 0,
  motor_status TEXT NOT NULL DEFAULT 'idle',     -- idle | running | stopped
  fault TEXT,
  estop INTEGER NOT NULL DEFAULT 0,              -- software emergency-stop latch
  hw_estop INTEGER NOT NULL DEFAULT 0,           -- physical e-stop as reported by device
  auto_mode INTEGER NOT NULL DEFAULT 0,
  sim_offline INTEGER NOT NULL DEFAULT 0,
  was_online INTEGER NOT NULL DEFAULT 0,
  last_seen REAL, last_tick REAL, last_logged REAL,
  job_id INTEGER, job_phase TEXT, job_started REAL, job_mode TEXT, phase_until REAL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sensor_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  drain_id INTEGER,
  water_level REAL, waste_level REAL, motor_status TEXT,
  source TEXT NOT NULL,                          -- 'simulated' or 'device'
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_dev ON sensor_readings(device_id, id);
CREATE TABLE IF NOT EXISTS cleaning_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  drain_id INTEGER, device_id TEXT,
  mode TEXT NOT NULL,                            -- automatic | manual
  started_at TEXT NOT NULL, ended_at TEXT,
  waste_before REAL, waste_after REAL,
  result TEXT NOT NULL DEFAULT 'in_progress',    -- in_progress | success | failed | aborted
  notes TEXT,
  simulated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS device_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  command TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',         -- queued | sent | received | completed | failed
  created_at TEXT NOT NULL, ack_at TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  level TEXT NOT NULL,                           -- info | warning | critical
  title TEXT NOT NULL,
  message TEXT,
  drain_id INTEGER, device_id TEXT,
  acknowledged INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
"""

# DEMO placeholders. These are NOT real drain locations - the coordinates are just
# approximate neighbourhood points so the map has something to show.
DEMO_DRAINS = [
    # code, name, location, lat, lng, water, waste, device, auto_mode
    ("DEMO-001", "DEMO Drain A (sample)", "Central Chennai area - DEMO placeholder", 13.0827, 80.2707, 25, 20, "SIM-001", 1),
    ("DEMO-002", "DEMO Drain B (sample)", "T. Nagar area - DEMO placeholder", 13.0418, 80.2341, 50, 52, "SIM-002", 0),
    ("DEMO-003", "DEMO Drain C (sample)", "Adyar area - DEMO placeholder", 13.0012, 80.2565, 78, 85, None, 0),
]


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def create_alert(db, level, title, message="", drain_id=None, device_id=None, dedupe=True):
    """Insert an alert. With dedupe, skip if the same alert is unacknowledged or was raised in the last 2 minutes."""
    if dedupe:
        row = db.execute(
            """SELECT id FROM alerts WHERE title=? AND IFNULL(drain_id,0)=IFNULL(?,0)
               AND IFNULL(device_id,'')=IFNULL(?,'')
               AND (acknowledged=0 OR created_at >= datetime('now','localtime','-120 seconds'))""",
            (title, drain_id, device_id)).fetchone()
        if row:
            return row["id"]
    cur = db.execute(
        "INSERT INTO alerts(level,title,message,drain_id,device_id,created_at) VALUES (?,?,?,?,?,?)",
        (level, title, message, drain_id, device_id, ts()))
    return cur.lastrowid


def init_db(reset=False):
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
    if reset and os.path.exists(config.DB_PATH):
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(config.DB_PATH + ext):
                os.remove(config.DB_PATH + ext)
    conn = connect()
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        conn.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES (?,?,?,?)",
                     (config.ADMIN_USER, generate_password_hash(config.ADMIN_PASSWORD), "admin", ts()))
    if conn.execute("SELECT COUNT(*) FROM drains").fetchone()[0] == 0:
        seed_demo(conn)
    conn.commit()
    conn.close()


def seed_demo(conn):
    from hardware import evaluate_status   # local import avoids a circular import
    now = time.time()
    for code, name, loc, lat, lng, water, waste, dev, auto in DEMO_DRAINS:
        status = evaluate_status(waste, False, False)
        cur = conn.execute(
            """INSERT INTO drains(drain_code,name,location,latitude,longitude,water_level,waste_level,status,
               is_demo,verified,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,1,0,?,?,?)""",
            (code, name, loc, lat, lng, water, waste, status, "DEMO sample data", ts(), ts()))
        if dev:
            conn.execute(
                """INSERT INTO hardware_devices(device_id,name,device_type,drain_id,water_level,waste_level,
                   basket_level,auto_mode,was_online,last_seen,last_tick,created_at)
                   VALUES (?,?,?,?,?,?,?,?,1,?,?,?)""",
                (dev, f"Simulated cleaner {dev}", "simulated", cur.lastrowid, water, waste, 10, auto, now, now, ts()))
            # back-fill ~20 minutes of simulated history so the charts are not empty on first run
            w = max(5, waste - 18)
            for i in range(40, 0, -1):
                w = max(2, min(100, w + random.uniform(-1.5, 2.2) + (18 / 40)))
                t = datetime.now() - timedelta(seconds=30 * i)
                conn.execute(
                    "INSERT INTO sensor_readings(device_id,drain_id,water_level,waste_level,motor_status,source,recorded_at) VALUES (?,?,?,?,?,?,?)",
                    (dev, cur.lastrowid, round(min(100, 15 + w * 0.65), 1), round(w, 1), "idle", "simulated", ts(t)))
    conn.execute(
        """INSERT INTO citizen_reports(report_id,drain_id,reporter_name,location_text,latitude,longitude,description,
           waste_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("DG-DEMO-0001", 3, "Demo Citizen", "Adyar area (DEMO sample report)", 13.0012, 80.2565,
         "DEMO sample report: plastic bags are blocking the drain mouth.", "Plastic", "Pending", ts(), ts()))
