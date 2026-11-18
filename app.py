"""DrainGuard 2.0 - Flask application (routes only; logic lives in hardware.py / db.py / ai_analysis.py)."""
import csv
import hmac
import io
import json
import os
import re
import secrets
import time
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, request, session, render_template, send_from_directory, send_file, redirect
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

import ai_analysis
import config
import db as dbm
import hardware

WASTE_TYPES = ["Plastic", "Leaves", "Mud and soil", "Solid waste", "Other"]
REPORT_STATUSES = ["Pending", "Verified", "Cleaning Assigned", "Resolved"]
NAV = [("dashboard", "Dashboard", "/", "📊"), ("map", "Chennai Drain Map", "/map", "🗺️"),
       ("report", "Report a Blocked Drain", "/report", "📸"), ("monitoring", "Drain Monitoring", "/monitoring", "📡"),
       ("hardware", "Hardware Control Center", "/hardware", "⚙️"), ("history", "Cleaning History", "/history", "🧹"),
       ("alerts", "Alerts and Notifications", "/alerts", "🔔"), ("admin", "Administration", "/admin", "🛠️")]
_login_fails = {}   # ip -> [timestamps]


class ApiError(Exception):
    def __init__(self, msg, code=400, **extra):
        super().__init__(msg)
        self.msg, self.code, self.extra = msg, code, extra


# ------------------------------------------------------------------ validation helpers
def as_float(v, name, lo, hi):
    if isinstance(v, bool):
        raise ApiError(f"{name} must be a number")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ApiError(f"{name} must be a number")
    if f != f or not (lo <= f <= hi):
        raise ApiError(f"{name} must be between {lo} and {hi}")
    return f


def as_text(v, name, maxlen=200, required=True, minlen=1):
    s = "" if v is None else str(v).strip()
    s = re.sub(r"[\x00-\x08\x0b-\x1f]", "", s)
    if required and len(s) < minlen:
        raise ApiError(f"{name} is required" + (f" (at least {minlen} characters)" if minlen > 1 else ""))
    if len(s) > maxlen:
        raise ApiError(f"{name} is too long (max {maxlen} characters)")
    return s


def as_coords(lat, lng):
    return (as_float(lat, "latitude", *config.LAT_RANGE), as_float(lng, "longitude", *config.LNG_RANGE))


def json_body():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        raise ApiError("Expected a JSON object body")
    return d


def sniff_image(head):
    """Identify the real image type from its first bytes (do not trust the file name)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("uid"):
            raise ApiError("Please log in as an administrator first.", 401)
        return f(*a, **k)
    return wrapper


def device_auth(device_id):
    db = dbm.get_db()
    dev = hardware.get_dev(db, device_id)
    key = request.headers.get("X-Device-Key", "")
    if not dev or dev["device_type"] != "real" or not dev["api_key"] or not hmac.compare_digest(key.encode(), dev["api_key"].encode()):
        raise ApiError("Invalid device ID or device key.", 401)
    return dev


def create_app():
    app = Flask(__name__)
    app.config.update(SECRET_KEY=config.SECRET_KEY, MAX_CONTENT_LENGTH=(config.MAX_UPLOAD_MB + 1) * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    dbm.init_db()
    app.teardown_appcontext(dbm.close_db)

    # ---------------------------------------------------------------- errors / hooks
    @app.errorhandler(ApiError)
    def _api_error(e):
        return jsonify(error=e.msg, **e.extra), e.code

    @app.errorhandler(hardware.CommandError)
    def _cmd_error(e):
        dbm.get_db().commit()
        return jsonify(error=e.msg, **e.extra), e.code

    @app.errorhandler(413)
    def _too_big(_e):
        return jsonify(error=f"File too large (max {config.MAX_UPLOAD_MB} MB)."), 413

    @app.errorhandler(404)
    def _404(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found"), 404
        return render_template("login.html", page="login", not_found=True), 404

    @app.errorhandler(Exception)
    def _500(e):
        app.logger.exception(e)
        try:
            dbm.get_db().rollback()
        except Exception:
            pass
        return jsonify(error="Internal server error"), 500

    @app.before_request
    def _tick():
        if request.method == "GET" and request.path.startswith("/api/") and not request.path.startswith("/api/uploads"):
            hardware.monitor_tick(dbm.get_db())

    @app.after_request
    def _after(resp):
        if "db" in dbm.g:
            dbm.get_db().commit() if resp.status_code < 500 else dbm.get_db().rollback()
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def _ctx():
        return {"NAV": NAV, "DEMO_MODE": config.DEMO_MODE}

    # ---------------------------------------------------------------- pages
    def page(name, title):
        return render_template(f"{name}.html", page=name, title=title)

    @app.get("/")
    def p_dashboard(): return page("dashboard", "Dashboard")

    @app.get("/map")
    def p_map(): return page("map", "Chennai Drain Map")

    @app.get("/report")
    def p_report(): return page("report", "Report a Blocked Drain")

    @app.get("/monitoring")
    def p_monitoring(): return page("monitoring", "Drain Monitoring")

    @app.get("/hardware")
    def p_hardware(): return page("hardware", "Hardware Control Center")

    @app.get("/history")
    def p_history(): return page("history", "Cleaning History")

    @app.get("/alerts")
    def p_alerts(): return page("alerts", "Alerts and Notifications")

    @app.get("/admin")
    def p_admin(): return page("admin", "Administration")

    @app.get("/login")
    def p_login(): return render_template("login.html", page="login", title="Admin login")

    @app.get("/sample-csv")
    def sample_csv():
        return send_file(os.path.join(config.BASE_DIR, "sample_data", "drain_import_template.csv"), as_attachment=True)

    # ---------------------------------------------------------------- auth
    @app.post("/api/login")
    def api_login():
        d = json_body()
        ip = request.remote_addr or "?"
        recent = [t for t in _login_fails.get(ip, []) if time.time() - t < 300]
        if len(recent) >= 5:
            raise ApiError("Too many failed attempts. Wait 5 minutes.", 429)
        row = dbm.get_db().execute("SELECT * FROM users WHERE username=?", (str(d.get("username", "")),)).fetchone()
        if not row or not check_password_hash(row["password_hash"], str(d.get("password", ""))):
            _login_fails[ip] = recent + [time.time()]
            raise ApiError("Wrong username or password.", 401)
        session.clear()
        session["uid"], session["username"], session["role"] = row["id"], row["username"], row["role"]
        return jsonify(ok=True, username=row["username"])

    @app.post("/api/logout")
    def api_logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/me")
    def api_me():
        return jsonify(logged_in=bool(session.get("uid")), username=session.get("username"), role=session.get("role"),
                       default_password=bool(session.get("uid")) and config.ADMIN_PASSWORD == "admin123")

    # ---------------------------------------------------------------- drains
    DRAIN_SQL = """SELECT d.*, h.device_id AS device_id,
        (SELECT COUNT(*) FROM citizen_reports r WHERE r.drain_id=d.id AND r.status!='Resolved') AS open_reports
        FROM drains d LEFT JOIN hardware_devices h ON h.drain_id=d.id"""

    def drain_dict(r):
        d = dict(r)
        d["is_demo"], d["verified"] = bool(d["is_demo"]), bool(d["verified"])
        return d

    def all_drains(db, status=None, q=None):
        sql, args, where = DRAIN_SQL, [], []
        if status:
            where.append("d.status=?"); args.append(status)
        if q:
            where.append("(d.drain_code LIKE ? OR d.name LIKE ? OR d.location LIKE ?)"); args += [f"%{q}%"] * 3
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [drain_dict(r) for r in db.execute(sql + " GROUP BY d.id ORDER BY d.id", args).fetchall()]

    @app.get("/api/drains")
    def api_drains():
        return jsonify(drains=all_drains(dbm.get_db(), request.args.get("status"), request.args.get("q", "").strip()[:60] or None))

    @app.post("/api/drains")
    @login_required
    def api_add_drain():
        d, db = json_body(), dbm.get_db()
        lat, lng = as_coords(d.get("latitude"), d.get("longitude"))
        name = as_text(d.get("name"), "name", 100)
        location = as_text(d.get("location"), "location", 200)
        source = as_text(d.get("source"), "source", 100, required=False) or "added by administrator"
        code = as_text(d.get("drain_code"), "drain_code", 30, required=False)
        if code and not re.fullmatch(r"[A-Za-z0-9_-]{2,30}", code):
            raise ApiError("drain_code may only contain letters, digits, - and _")
        if not code:
            code = f"DG-D{db.execute('SELECT COUNT(*) FROM drains').fetchone()[0] + 1:03d}"
            while db.execute("SELECT 1 FROM drains WHERE drain_code=?", (code,)).fetchone():
                code += "x"
        if db.execute("SELECT 1 FROM drains WHERE drain_code=?", (code,)).fetchone():
            raise ApiError(f"A drain with code {code} already exists.", 409)
        cur = db.execute("""INSERT INTO drains(drain_code,name,location,latitude,longitude,is_demo,verified,source,created_at,updated_at)
                            VALUES (?,?,?,?,?,0,?,?,?,?)""",
                         (code, name, location, lat, lng, 1 if d.get("verified") is True else 0, source, dbm.ts(), dbm.ts()))
        return jsonify(ok=True, drain=drain_dict(db.execute(DRAIN_SQL + " WHERE d.id=?", (cur.lastrowid,)).fetchone())), 201

    @app.post("/api/drains/import")
    @login_required
    def api_import_drains():
        f = request.files.get("file")
        if not f or not f.filename.lower().endswith(".csv"):
            raise ApiError("Upload a .csv file (see the template).")
        try:
            text = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ApiError("CSV must be UTF-8 encoded.")
        db, added, errors = dbm.get_db(), 0, []
        rows = list(csv.DictReader(io.StringIO(text)))
        if len(rows) > 1000:
            raise ApiError("Maximum 1000 rows per import.")
        for i, row in enumerate(rows, start=2):
            try:
                row = {(k or "").strip().lower(): v for k, v in row.items()}
                lat, lng = as_coords(row.get("latitude"), row.get("longitude"))
                code = as_text(row.get("drain_code"), "drain_code", 30)
                if not re.fullmatch(r"[A-Za-z0-9_-]{2,30}", code):
                    raise ApiError("invalid drain_code")
                if db.execute("SELECT 1 FROM drains WHERE drain_code=?", (code,)).fetchone():
                    raise ApiError(f"{code} already exists")
                db.execute("""INSERT INTO drains(drain_code,name,location,latitude,longitude,is_demo,verified,source,created_at,updated_at)
                              VALUES (?,?,?,?,?,0,1,?,?,?)""",
                           (code, as_text(row.get("name"), "name", 100), as_text(row.get("location"), "location", 200), lat, lng,
                            as_text(row.get("source"), "source", 100, required=False) or f"CSV import {f.filename[:40]}", dbm.ts(), dbm.ts()))
                added += 1
            except ApiError as e:
                errors.append(f"Row {i}: {e.msg}")
        return jsonify(ok=True, added=added, errors=errors[:30], error_count=len(errors))

    # ---------------------------------------------------------------- images + reports
    def image_dict(r):
        return {"id": r["id"], "url": f"/api/uploads/{r['filename']}", "uploaded_at": r["uploaded_at"], "report_id": r["report_id"],
                "analysis": {"simulated": r["ai_mode"] == "simulated", "mode": r["ai_mode"],
                             "categories": json.loads(r["ai_categories"] or "[]"), "severity": r["ai_severity"],
                             "confidence": r["ai_confidence"], "suggested_action": r["ai_action"],
                             "notice": ai_analysis.NOTICE if r["ai_mode"] == "simulated" else None}}

    @app.post("/api/upload")
    def api_upload():
        f = request.files.get("image")
        if not f or not f.filename:
            raise ApiError("No image selected.")
        original = secure_filename(f.filename) or "image"
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if ext not in config.ALLOWED_EXTENSIONS:
            raise ApiError("Only PNG, JPG and WEBP images are allowed.")
        data = f.read(config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            raise ApiError(f"Image too large (max {config.MAX_UPLOAD_MB} MB).", 413)
        real = sniff_image(data[:16])
        if not real:
            raise ApiError("That file is not a valid PNG, JPG or WEBP image.")
        name = f"{secrets.token_hex(12)}.{real}"      # server-generated name, never the user's
        path = os.path.join(config.UPLOAD_FOLDER, name)
        with open(path, "wb") as out:
            out.write(data)
        waste_type = request.form.get("waste_type")
        result = ai_analysis.analyze_image(path, waste_type if waste_type in WASTE_TYPES else None)
        db = dbm.get_db()
        cur = db.execute("""INSERT INTO uploaded_images(filename,original_name,size_bytes,ai_mode,ai_categories,ai_severity,
                            ai_confidence,ai_action,uploaded_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                         (name, original[:80], len(data), result["mode"], json.dumps(result["categories"]), result["severity"],
                          result["confidence"], result["suggested_action"], dbm.ts()))
        row = db.execute("SELECT * FROM uploaded_images WHERE id=?", (cur.lastrowid,)).fetchone()
        return jsonify(ok=True, image=image_dict(row)), 201

    @app.get("/api/uploads/<path:filename>")
    def api_uploaded_file(filename):
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in config.ALLOWED_EXTENSIONS or not re.fullmatch(r"[0-9a-f]{24}\.(png|jpg|webp)", filename):
            return jsonify(error="Not found"), 404
        resp = send_from_directory(config.UPLOAD_FOLDER, filename, mimetype={"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[ext])
        resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return resp

    def report_dict(db, r):
        d = dict(r)
        d.pop("reporter_name", None) if not session.get("uid") else None
        imgs = db.execute("SELECT * FROM uploaded_images WHERE report_id=? ORDER BY id", (r["report_id"],)).fetchall()
        d["images"] = [image_dict(i) for i in imgs]
        return d

    @app.get("/api/reports")
    def api_reports():
        db, args, sql = dbm.get_db(), [], "SELECT * FROM citizen_reports"
        status = request.args.get("status")
        if status in REPORT_STATUSES:
            sql += " WHERE status=?"; args.append(status)
        rows = db.execute(sql + " ORDER BY id DESC LIMIT ?", args + [min(request.args.get("limit", 100, type=int), 500)]).fetchall()
        return jsonify(reports=[report_dict(db, r) for r in rows])

    @app.post("/api/reports")
    def api_create_report():
        d, db = json_body(), dbm.get_db()
        waste_type = d.get("waste_type")
        if waste_type not in WASTE_TYPES:
            raise ApiError("Choose a waste type: " + ", ".join(WASTE_TYPES))
        description = as_text(d.get("description"), "description", 1000, minlen=5)
        location_text = as_text(d.get("location_text"), "location", 200, required=False)
        lat = lng = None
        if d.get("latitude") not in (None, "") or d.get("longitude") not in (None, ""):
            lat, lng = as_coords(d.get("latitude"), d.get("longitude"))
        drain_id = d.get("drain_id")
        if drain_id not in (None, ""):
            row = db.execute("SELECT id, latitude, longitude, location FROM drains WHERE id=?", (drain_id,)).fetchone()
            if not row:
                raise ApiError("Selected drain does not exist.")
            drain_id = row["id"]
            if lat is None:
                lat, lng = row["latitude"], row["longitude"]
            location_text = location_text or row["location"]
        else:
            drain_id = None
        if not location_text and lat is None:
            raise ApiError("Enter the drain location or pick a point on the map.")
        image_ids = d.get("image_ids") or []
        if not isinstance(image_ids, list) or len(image_ids) > config.MAX_IMAGES_PER_REPORT:
            raise ApiError(f"Attach at most {config.MAX_IMAGES_PER_REPORT} images.")
        imgs = []
        for i in image_ids:
            r = db.execute("SELECT * FROM uploaded_images WHERE id=? AND report_id IS NULL", (i,)).fetchone() if isinstance(i, int) else None
            if not r:
                raise ApiError("An attached image was not found or is already used. Upload it again.")
            imgs.append(r)
        for _ in range(10):
            report_id = f"DG-{datetime.now():%Y%m%d}-{secrets.token_hex(2).upper()}"
            if not db.execute("SELECT 1 FROM citizen_reports WHERE report_id=?", (report_id,)).fetchone():
                break
        db.execute("""INSERT INTO citizen_reports(report_id,drain_id,reporter_name,location_text,latitude,longitude,description,
                      waste_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?, 'Pending', ?, ?)""",
                   (report_id, drain_id, as_text(d.get("reporter_name"), "name", 60, required=False) or None, location_text,
                    lat, lng, description, waste_type, dbm.ts(), dbm.ts()))
        for r in imgs:
            db.execute("UPDATE uploaded_images SET report_id=? WHERE id=?", (report_id, r["id"]))
        high = any(r["ai_severity"] == "High" for r in imgs)
        dbm.create_alert(db, "warning" if high else "info", f"New citizen report {report_id}",
                         f"{waste_type} blockage reported at {location_text or 'a map point'}."
                         + (" (Simulated AI severity: High)" if high and imgs[0]["ai_mode"] == "simulated" else ""),
                         drain_id, None, dedupe=False)
        row = db.execute("SELECT * FROM citizen_reports WHERE report_id=?", (report_id,)).fetchone()
        return jsonify(ok=True, report=report_dict(db, row)), 201

    @app.patch("/api/reports/<report_id>")
    @login_required
    def api_update_report(report_id):
        d, db = json_body(), dbm.get_db()
        if d.get("status") not in REPORT_STATUSES:
            raise ApiError("status must be one of: " + ", ".join(REPORT_STATUSES))
        cur = db.execute("UPDATE citizen_reports SET status=?, updated_at=? WHERE report_id=?", (d["status"], dbm.ts(), report_id))
        if cur.rowcount == 0:
            raise ApiError("Report not found.", 404)
        return jsonify(ok=True)

    # ---------------------------------------------------------------- hardware (dashboard side)
    @app.get("/api/hardware")
    def api_hardware():
        db = dbm.get_db()
        devs = [hardware.device_view(db, dict(r)) for r in db.execute("SELECT * FROM hardware_devices ORDER BY id").fetchall()]
        return jsonify(devices=devs, thresholds={k: getattr(config, k) for k in (
            "WARNING_WASTE", "BLOCKED_WASTE", "CRITICAL_WASTE", "CLEAN_TARGET", "VERIFY_MAX_WASTE", "BASKET_FULL",
            "MOTOR_TIMEOUT_S", "HEARTBEAT_TIMEOUT_S")})

    @app.post("/api/hardware/command")
    def api_hw_command():
        d = json_body()
        command = d.get("command")
        # Emergency stop is deliberately available without login: it can only make the system safer.
        if command != "emergency_stop" and not session.get("uid"):
            raise ApiError("Please log in as an administrator to control hardware.", 401)
        out = hardware.handle_command(dbm.get_db(), as_text(d.get("device_id"), "device_id", 40), command,
                                      confirm=d.get("confirm") is True, value=d.get("value"))
        return jsonify(out)

    @app.post("/api/hardware/register")
    @login_required
    def api_hw_register():
        d, db = json_body(), dbm.get_db()
        device_id = as_text(d.get("device_id"), "device_id", 32)
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", device_id):
            raise ApiError("device_id: 3-32 letters, digits, - or _")
        if db.execute("SELECT 1 FROM hardware_devices WHERE device_id=?", (device_id,)).fetchone():
            raise ApiError("That device ID is already registered.", 409)
        drain_id = d.get("drain_id")
        if drain_id not in (None, "") and not db.execute("SELECT 1 FROM drains WHERE id=?", (drain_id,)).fetchone():
            raise ApiError("Selected drain does not exist.")
        key = hardware.register_device(db, device_id, as_text(d.get("name"), "name", 80, required=False) or device_id, drain_id or None)
        return jsonify(ok=True, device_id=device_id, api_key=key, note="Store this key on the device now - it is not shown again."), 201

    # ---------------------------------------------------------------- hardware (device side, HTTP)
    @app.post("/api/sensor-data")
    def api_sensor_data():
        d = json_body()
        dev = device_auth(as_text(d.get("device_id"), "device_id", 40))
        data = {"water_level": as_float(d.get("water_level"), "water_level", 0, 100),
                "waste_level": as_float(d.get("waste_level"), "waste_level", 0, 100), "hw_estop": d.get("hw_estop") is True,
                "fault": as_text(d.get("fault"), "fault", 200, required=False)}
        if d.get("basket_level") is not None:
            data["basket_level"] = as_float(d["basket_level"], "basket_level", 0, 100)
        if d.get("motor_status") in ("idle", "running", "stopped"):
            data["motor_status"] = d["motor_status"]
        hardware.process_reading(dbm.get_db(), dev, data)
        return jsonify(ok=True, server_time=dbm.ts())

    @app.post("/api/device/<device_id>/heartbeat")
    def api_heartbeat(device_id):
        dev = device_auth(device_id)
        dbm.get_db().execute("UPDATE hardware_devices SET last_seen=? WHERE device_id=?", (time.time(), dev["device_id"]))
        return jsonify(ok=True, server_time=dbm.ts())

    @app.get("/api/device/<device_id>/commands")
    def api_device_commands(device_id):
        dev, db = device_auth(device_id), dbm.get_db()
        db.execute("UPDATE hardware_devices SET last_seen=? WHERE device_id=?", (time.time(), dev["device_id"]))
        rows = db.execute("SELECT id, command FROM device_commands WHERE device_id=? AND status='queued' ORDER BY id", (device_id,)).fetchall()
        db.execute("UPDATE device_commands SET status='sent' WHERE device_id=? AND status='queued'", (device_id,))
        return jsonify(commands=[dict(r) for r in rows])

    @app.post("/api/device/<device_id>/ack")
    def api_device_ack(device_id):
        d, dev = json_body(), device_auth(device_id)
        if not isinstance(d.get("command_id"), int):
            raise ApiError("command_id (integer) is required.")
        hardware.handle_ack(dbm.get_db(), dev, d["command_id"], d.get("status"), as_text(d.get("message"), "message", 200, required=False))
        return jsonify(ok=True)

    # ---------------------------------------------------------------- history / alerts / readings / dashboard
    @app.get("/api/history")
    def api_history():
        rows = dbm.get_db().execute("""SELECT c.*, d.drain_code FROM cleaning_history c LEFT JOIN drains d ON d.id=c.drain_id
                                       ORDER BY c.id DESC LIMIT ?""", (min(request.args.get("limit", 100, type=int), 500),)).fetchall()
        return jsonify(history=[dict(r, simulated=bool(r["simulated"])) for r in rows])

    @app.get("/api/alerts")
    def api_alerts():
        sql = "SELECT * FROM alerts" + (" WHERE acknowledged=0" if request.args.get("unack") == "1" else "")
        rows = dbm.get_db().execute(sql + " ORDER BY id DESC LIMIT ?", (min(request.args.get("limit", 100, type=int), 500),)).fetchall()
        return jsonify(alerts=[dict(r, acknowledged=bool(r["acknowledged"])) for r in rows])

    @app.post("/api/alerts/<int:alert_id>/ack")
    @login_required
    def api_ack_alert(alert_id):
        cur = dbm.get_db().execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))
        if cur.rowcount == 0:
            raise ApiError("Alert not found.", 404)
        return jsonify(ok=True)

    @app.post("/api/alerts/ack-all")
    @login_required
    def api_ack_all():
        dbm.get_db().execute("UPDATE alerts SET acknowledged=1")
        return jsonify(ok=True)

    @app.get("/api/readings")
    def api_readings():
        device_id, drain_id = request.args.get("device_id"), request.args.get("drain_id", type=int)
        where, args = [], []
        if device_id:
            where.append("device_id=?"); args.append(device_id)
        if drain_id:
            where.append("drain_id=?"); args.append(drain_id)
        sql = "SELECT recorded_at, water_level, waste_level, motor_status, source FROM sensor_readings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = dbm.get_db().execute(sql + " ORDER BY id DESC LIMIT ?", args + [min(request.args.get("limit", 60, type=int), 500)]).fetchall()
        return jsonify(readings=[dict(r) for r in reversed(rows)])

    @app.get("/api/dashboard")
    def api_dashboard():
        db = dbm.get_db()
        n = lambda sql: db.execute(sql).fetchone()[0]
        devs = [hardware.device_view(db, dict(r)) for r in db.execute("SELECT * FROM hardware_devices ORDER BY id").fetchall()]
        imgs = db.execute("SELECT * FROM uploaded_images ORDER BY id DESC LIMIT 6").fetchall()
        return jsonify(
            stats={"total_drains": n("SELECT COUNT(*) FROM drains"),
                   "normal": n("SELECT COUNT(*) FROM drains WHERE status='normal'"),
                   "warning": n("SELECT COUNT(*) FROM drains WHERE status='warning'"),
                   "blocked": n("SELECT COUNT(*) FROM drains WHERE status IN ('blocked','assistance')"),
                   "cleaning": n("SELECT COUNT(*) FROM drains WHERE status='cleaning'"),
                   "pending_reports": n("SELECT COUNT(*) FROM citizen_reports WHERE status='Pending'"),
                   "cleanings_completed": n("SELECT COUNT(*) FROM cleaning_history WHERE result='success'"),
                   "devices_total": len(devs), "devices_online": sum(1 for d in devs if d["online"])},
            drains=all_drains(db), devices=devs,
            recent_images=[image_dict(i) for i in imgs],
            recent_alerts=[dict(r, acknowledged=bool(r["acknowledged"])) for r in db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 6")],
            recent_history=[dict(r) for r in db.execute("""SELECT c.*, d.drain_code FROM cleaning_history c
                            LEFT JOIN drains d ON d.id=c.drain_id ORDER BY c.id DESC LIMIT 5""")])

    return app


app = create_app()

if __name__ == "__main__":
    print("\n  DrainGuard 2.0 running at http://127.0.0.1:5000  (admin login: %s / %s - change it!)\n" % (config.ADMIN_USER, config.ADMIN_PASSWORD))
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
