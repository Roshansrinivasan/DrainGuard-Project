"""
Hardware communication + cleaning controller.

One code path serves both kinds of devices:
  * 'simulated' devices are advanced by simulate_tick()/monitor_tick() (software only - NOT real motor control)
  * 'real' devices (ESP32/Arduino) talk over HTTP: they POST sensor data, poll for queued commands and ACK them.

SAFETY DESIGN
  * The dashboard can only *request* actions. A real device must keep its own physical emergency stop,
    current limits and interlocks. Software never overrides a physical e-stop (hw_estop).
  * Faults are latched until a human presses "Reset device".
  * Motor time-out: a cleaning cycle that runs longer than MOTOR_TIMEOUT_S is aborted -> human assistance.
"""
import random
import secrets
import threading
import time
from datetime import datetime

import config
from db import create_alert, ts

_lock = threading.RLock()
MECH_CYCLE = ["lowering rake", "raking debris", "lifting rake", "emptying into basket"]
SIM_COMMANDS = {"sim_add_waste", "sim_fault", "sim_toggle_offline", "sim_toggle_hw_estop", "sim_basket_emptied"}


class CommandError(Exception):
    def __init__(self, msg, code=400, **extra):
        super().__init__(msg)
        self.msg, self.code, self.extra = msg, code, extra


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def evaluate_status(waste, fault, cleaning):
    """Normal / Warning / Blocked / Cleaning / Human assistance."""
    if fault or waste >= config.CRITICAL_WASTE:
        return "assistance"
    if cleaning:
        return "cleaning"
    if waste >= config.BLOCKED_WASTE:
        return "blocked"
    if waste >= config.WARNING_WASTE:
        return "warning"
    return "normal"


def get_dev(db, device_id):
    r = db.execute("SELECT * FROM hardware_devices WHERE device_id=?", (device_id,)).fetchone()
    return dict(r) if r else None


def is_online(dev, now=None):
    now = now or time.time()
    if dev["device_type"] == "simulated" and dev["sim_offline"]:
        return False
    return bool(dev["last_seen"]) and (now - dev["last_seen"]) <= config.HEARTBEAT_TIMEOUT_S


def mechanism_status(dev, now):
    if dev["fault"]:
        return "fault - locked"
    if dev["estop"] or dev["hw_estop"]:
        return "locked (emergency stop)"
    if dev["job_id"] is not None and dev["job_phase"] == "cleaning":
        return MECH_CYCLE[int((now - (dev["job_started"] or now)) / 2.5) % 4]
    if dev["job_id"] is not None:
        return "verifying"
    return "idle (parked)"


def device_view(db, dev, now=None):
    now = now or time.time()
    drain = None
    if dev["drain_id"]:
        drain = db.execute("SELECT drain_code,name FROM drains WHERE id=?", (dev["drain_id"],)).fetchone()
    cleaning = dev["job_id"] is not None
    basket = dev["basket_level"]
    return {
        "device_id": dev["device_id"], "name": dev["name"], "type": dev["device_type"],
        "simulated": dev["device_type"] == "simulated",
        "drain_id": dev["drain_id"], "drain_code": drain["drain_code"] if drain else None,
        "drain_name": drain["name"] if drain else None,
        "online": is_online(dev, now),
        "last_seen": ts(datetime.fromtimestamp(dev["last_seen"])) if dev["last_seen"] else None,
        "seconds_since": round(now - dev["last_seen"]) if dev["last_seen"] else None,
        "water_level": round(dev["water_level"], 1), "waste_level": round(dev["waste_level"], 1),
        "basket_level": round(basket, 1),
        "basket_status": "FULL - empty required" if basket >= config.BASKET_FULL else ("filling" if basket >= 60 else "ok"),
        "motor_status": dev["motor_status"], "mechanism_status": mechanism_status(dev, now),
        "fault": dev["fault"], "estop": bool(dev["estop"]), "hw_estop": bool(dev["hw_estop"]),
        "auto_mode": bool(dev["auto_mode"]),
        "status": evaluate_status(dev["waste_level"], bool(dev["fault"]), cleaning),
        "job": ({"phase": dev["job_phase"], "mode": dev["job_mode"], "elapsed": round(now - dev["job_started"]),
                 "timeout": config.MOTOR_TIMEOUT_S} if cleaning else None),
    }


def queue_command(db, device_id, command):
    cur = db.execute("INSERT INTO device_commands(device_id,command,created_at) VALUES (?,?,?)",
                     (device_id, command, ts()))
    return cur.lastrowid


def record_reading(db, dev, source):
    db.execute(
        "INSERT INTO sensor_readings(device_id,drain_id,water_level,waste_level,motor_status,source,recorded_at) VALUES (?,?,?,?,?,?,?)",
        (dev["device_id"], dev["drain_id"], round(dev["water_level"], 1), round(dev["waste_level"], 1),
         dev["motor_status"], source, ts()))
    if random.random() < 0.01:   # keep the table small
        db.execute("DELETE FROM sensor_readings WHERE id < (SELECT MAX(id) - 20000 FROM sensor_readings)")


# ----------------------------------------------------------------- drain sync
def sync_drain(db, dev):
    """Copy the device's latest readings to its drain and raise alerts on status changes."""
    if not dev["drain_id"]:
        return
    drain = db.execute("SELECT * FROM drains WHERE id=?", (dev["drain_id"],)).fetchone()
    if not drain:
        return
    new = evaluate_status(dev["waste_level"], bool(dev["fault"]), dev["job_id"] is not None)
    db.execute("UPDATE drains SET water_level=?, waste_level=?, status=?, updated_at=? WHERE id=?",
               (round(dev["water_level"], 1), round(dev["waste_level"], 1), new, ts(), drain["id"]))
    if new != drain["status"]:
        label = drain["drain_code"]
        if new == "blocked":
            create_alert(db, "warning", f"Drain blocked: {label}", f"Waste level reached {dev['waste_level']:.0f}%.", drain["id"], dev["device_id"])
        elif new == "assistance":
            create_alert(db, "critical", f"Human assistance needed: {label}",
                         dev["fault"] or f"Critical waste level {dev['waste_level']:.0f}%.", drain["id"], dev["device_id"])
        elif new == "warning" and drain["status"] == "normal":
            create_alert(db, "info", f"Waste building up: {label}", f"Waste level {dev['waste_level']:.0f}%.", drain["id"], dev["device_id"])


# ----------------------------------------------------------------- cleaning jobs
def safety_check(dev, now=None):
    """Return an error string if it is NOT safe to start the motor, else None."""
    if not is_online(dev, now):
        return "Device is offline - cleaning is not allowed."
    if dev["fault"]:
        return f"Device reports a fault ({dev['fault']}). Human assistance required; press Reset after inspection."
    if dev["estop"]:
        return "Software emergency stop is latched. Reset the device first."
    if dev["hw_estop"]:
        return "Physical emergency stop is engaged on the device. Release it on the hardware first."
    if dev["job_id"] is not None:
        return "A cleaning cycle is already in progress."
    if dev["basket_level"] >= config.BASKET_FULL:
        return "Waste basket is full. Empty it before cleaning."
    if dev["waste_level"] >= config.CRITICAL_WASTE:
        return "Critical reading - human assistance required before the machine may run."
    return None


def start_cleaning(db, device_id, mode):
    dev = get_dev(db, device_id)
    err = safety_check(dev)
    if err:
        return False, err
    now = time.time()
    sim = dev["device_type"] == "simulated"
    cur = db.execute(
        "INSERT INTO cleaning_history(drain_id,device_id,mode,started_at,waste_before,result,notes,simulated) VALUES (?,?,?,?,?,'in_progress','',?)",
        (dev["drain_id"], device_id, mode, ts(), dev["waste_level"], 1 if sim else 0))
    db.execute("""UPDATE hardware_devices SET job_id=?, job_phase='cleaning', job_started=?, job_mode=?,
                  phase_until=NULL, motor_status='running' WHERE device_id=?""", (cur.lastrowid, now, mode, device_id))
    if not sim:
        queue_command(db, device_id, "START_CLEANING")
    sync_drain(db, get_dev(db, device_id))
    note = "SIMULATED motor started (no real motor)." if sim else "START_CLEANING queued for the device."
    return True, f"{mode.capitalize()} cleaning started. {note}"


def finish_job(db, dev, result, notes):
    """Close the active cleaning job: write history, clear job fields, update basket."""
    if dev["job_id"] is None:
        return
    hist = db.execute("SELECT * FROM cleaning_history WHERE id=?", (dev["job_id"],)).fetchone()
    waste_after = dev["waste_level"]
    db.execute("UPDATE cleaning_history SET ended_at=?, waste_after=?, result=?, notes=? WHERE id=?",
               (ts(), round(waste_after, 1), result, notes, dev["job_id"]))
    basket = dev["basket_level"]
    if result == "success" and dev["device_type"] == "simulated" and hist:
        basket = clamp(basket + max(0, (hist["waste_before"] or 0) - waste_after) * 0.4)
    db.execute("""UPDATE hardware_devices SET job_id=NULL, job_phase=NULL, job_started=NULL, job_mode=NULL,
                  phase_until=NULL, motor_status=?, basket_level=? WHERE device_id=?""",
               ("idle" if result == "success" else "stopped", basket, dev["device_id"]))
    if result == "success":
        create_alert(db, "info", f"Cleaning completed: {dev['device_id']}",
                     f"Waste {hist['waste_before']:.0f}% -> {waste_after:.0f}%.", dev["drain_id"], dev["device_id"], dedupe=False)
    sync_drain(db, get_dev(db, dev["device_id"]))


def fail_job(db, dev, reason):
    """Cleaning failed: stop motor, latch fault, raise a human-assistance alert."""
    if dev["device_type"] != "simulated":
        queue_command(db, dev["device_id"], "STOP_MOTOR")
    finish_job(db, dev, "failed", reason)
    db.execute("UPDATE hardware_devices SET fault=?, motor_status='stopped' WHERE device_id=?", (reason[:200], dev["device_id"]))
    create_alert(db, "critical", f"Human assistance required: {dev['device_id']}",
                 f"Cleaning failed - {reason}. Motor stopped. Inspect the device, then press Reset.",
                 dev["drain_id"], dev["device_id"], dedupe=False)
    sync_drain(db, get_dev(db, dev["device_id"]))


def advance_job(db, device_id, now=None):
    """Move an active cleaning job forward (timeout / fault / verify checks)."""
    now = now or time.time()
    dev = get_dev(db, device_id)
    if not dev or dev["job_id"] is None:
        return
    if dev["fault"]:
        return fail_job(db, dev, dev["fault"])
    if dev["estop"] or dev["hw_estop"]:
        return finish_job(db, dev, "aborted", "Emergency stop engaged during cleaning")
    if now - (dev["job_started"] or now) > config.MOTOR_TIMEOUT_S:
        return fail_job(db, dev, f"Motor time-out: cycle exceeded {config.MOTOR_TIMEOUT_S}s")
    if not is_online(dev, now):
        return fail_job(db, dev, "Device went offline during cleaning")
    if dev["job_phase"] == "cleaning":
        if dev["waste_level"] <= config.CLEAN_TARGET:
            db.execute("UPDATE hardware_devices SET job_phase='verifying', phase_until=?, motor_status='idle' WHERE device_id=?",
                       (now + 2, device_id))
    elif dev["job_phase"] == "verifying" and now >= (dev["phase_until"] or 0):
        if dev["device_type"] == "real" and (dev["last_seen"] or 0) < dev["phase_until"]:
            return   # wait for a fresh sensor reading from the real device
        if dev["waste_level"] <= config.VERIFY_MAX_WASTE:
            finish_job(db, dev, "success", f"Verified: waste {dev['waste_level']:.0f}% after cleaning")
        else:
            fail_job(db, dev, f"Verification failed: waste still {dev['waste_level']:.0f}% after cleaning")


def maybe_auto_clean(db, device_id):
    dev = get_dev(db, device_id)
    if not dev["auto_mode"] or dev["job_id"] is not None or dev["waste_level"] < config.BLOCKED_WASTE:
        return
    err = safety_check(dev)
    if err:
        create_alert(db, "warning", f"Automatic cleaning blocked: {device_id}", err, dev["drain_id"], device_id)
        return
    start_cleaning(db, device_id, "automatic")


def emergency_stop(db, dev):
    if dev["job_id"] is not None:
        finish_job(db, dev, "aborted", "Emergency stop pressed on the dashboard")
    db.execute("UPDATE hardware_devices SET estop=1, motor_status='stopped' WHERE device_id=?", (dev["device_id"],))
    if dev["device_type"] != "simulated":
        queue_command(db, dev["device_id"], "EMERGENCY_STOP")
    create_alert(db, "warning", f"Emergency stop: {dev['device_id']}",
                 "Emergency stop latched from the dashboard. Press Reset after the area is safe.", dev["drain_id"], dev["device_id"], dedupe=False)


def reset_device(db, dev):
    if dev["hw_estop"]:
        raise CommandError("The PHYSICAL emergency stop is still engaged on the device. Release it on the hardware - "
                           "software cannot override it.", 409)
    if not is_online(dev):
        raise CommandError("Device is offline - cannot reset.", 409)
    if dev["job_id"] is not None:
        finish_job(db, dev, "aborted", "Device reset during cleaning")
    db.execute("UPDATE hardware_devices SET estop=0, fault=NULL, motor_status='idle' WHERE device_id=?", (dev["device_id"],))
    if dev["device_type"] != "simulated":
        queue_command(db, dev["device_id"], "RESET")


# ----------------------------------------------------------------- command dispatcher
def handle_command(db, device_id, command, confirm=False, value=None):
    dev = get_dev(db, device_id)
    if not dev:
        raise CommandError("Unknown device.", 404)
    sim = dev["device_type"] == "simulated"
    if command == "emergency_stop":
        emergency_stop(db, dev)
        msg = "EMERGENCY STOP latched. Motor commanded off."
    elif command == "start_auto_clean":
        if dev["waste_level"] < config.BLOCKED_WASTE:
            raise CommandError(f"Automatic cleaning starts only when waste is at least {config.BLOCKED_WASTE}% "
                               f"(now {dev['waste_level']:.0f}%).", 409)
        ok, msg = start_cleaning(db, device_id, "automatic")
        if not ok:
            raise CommandError(msg, 409)
    elif command == "start_manual_clean":
        if confirm is not True:
            raise CommandError("Confirmation is required before manually activating the motor.", 400, needs_confirmation=True)
        ok, msg = start_cleaning(db, device_id, "manual")
        if not ok:
            raise CommandError(msg, 409)
    elif command == "reset_device":
        reset_device(db, dev)
        msg = "Device status reset (software latch and fault cleared)."
    elif command == "set_auto_mode":
        if not isinstance(value, bool):
            raise CommandError("value must be true or false.", 400)
        db.execute("UPDATE hardware_devices SET auto_mode=? WHERE device_id=?", (1 if value else 0, device_id))
        msg = f"Automatic mode {'ENABLED' if value else 'DISABLED'}."
    elif command in SIM_COMMANDS:
        if not sim:
            raise CommandError("That control only exists for simulated devices.", 400)
        msg = _sim_command(db, dev, command)
    else:
        raise CommandError("Unknown command.", 400)
    dev = get_dev(db, device_id)
    sync_drain(db, dev)
    if dev["auto_mode"] and command in ("sim_add_waste", "set_auto_mode"):
        maybe_auto_clean(db, device_id)
    db.commit()
    return {"ok": True, "message": msg, "device": device_view(db, get_dev(db, device_id))}


def _sim_command(db, dev, command):
    did = dev["device_id"]
    if command == "sim_add_waste":
        if dev["job_id"] is not None:
            raise CommandError("Wait for the current cleaning cycle to finish.", 409)
        w = min(90.0, max(dev["waste_level"] + 25, 75.0))
        db.execute("UPDATE hardware_devices SET waste_level=?, water_level=? WHERE device_id=?", (w, clamp(15 + w * 0.65), did))
        return f"SIMULATION: blockage added (waste now {w:.0f}%)."
    if command == "sim_fault":
        if dev["job_id"] is not None:
            fail_job(db, dev, "Simulated motor jam (demo)")
        else:
            db.execute("UPDATE hardware_devices SET fault='Simulated motor jam (demo)', motor_status='stopped' WHERE device_id=?", (did,))
            create_alert(db, "critical", f"Human assistance required: {did}", "Simulated mechanical fault.", dev["drain_id"], did, dedupe=False)
        return "SIMULATION: mechanical fault injected."
    if command == "sim_toggle_offline":
        db.execute("UPDATE hardware_devices SET sim_offline=? WHERE device_id=?", (0 if dev["sim_offline"] else 1, did))
        if dev["sim_offline"]:
            db.execute("UPDATE hardware_devices SET last_seen=? WHERE device_id=?", (time.time(), did))
        return "SIMULATION: device brought back online." if dev["sim_offline"] else "SIMULATION: device set to offline."
    if command == "sim_toggle_hw_estop":
        db.execute("UPDATE hardware_devices SET hw_estop=? WHERE device_id=?", (0 if dev["hw_estop"] else 1, did))
        advance_job(db, did)
        return "SIMULATION: physical e-stop " + ("released." if dev["hw_estop"] else "pressed.")
    if command == "sim_basket_emptied":
        db.execute("UPDATE hardware_devices SET basket_level=0 WHERE device_id=?", (did,))
        return "SIMULATION: basket marked as emptied."


# ----------------------------------------------------------------- real-device HTTP integration
def register_device(db, device_id, name, drain_id):
    api_key = secrets.token_urlsafe(24)
    db.execute("""INSERT INTO hardware_devices(device_id,name,device_type,drain_id,api_key,created_at)
                  VALUES (?,?, 'real', ?, ?, ?)""", (device_id, name, drain_id, api_key, ts()))
    return api_key


def process_reading(db, dev, data):
    """Handle one POST /api/sensor-data from a real device (values already validated by the caller)."""
    now = time.time()
    db.execute("""UPDATE hardware_devices SET water_level=?, waste_level=?, last_seen=?,
                  motor_status=COALESCE(?, motor_status), basket_level=COALESCE(?, basket_level), hw_estop=? WHERE device_id=?""",
               (data["water_level"], data["waste_level"], now, data.get("motor_status"), data.get("basket_level"),
                1 if data.get("hw_estop") else 0, dev["device_id"]))
    if data.get("fault"):   # faults only latch; clearing needs a human Reset
        db.execute("UPDATE hardware_devices SET fault=? WHERE device_id=?", (str(data["fault"])[:200], dev["device_id"]))
    dev = get_dev(db, dev["device_id"])
    record_reading(db, dev, "device")
    advance_job(db, dev["device_id"], now)
    sync_drain(db, get_dev(db, dev["device_id"]))
    maybe_auto_clean(db, dev["device_id"])


def handle_ack(db, dev, command_id, status, message):
    if status not in ("received", "completed", "failed"):
        raise CommandError("status must be received, completed or failed.", 400)
    row = db.execute("SELECT * FROM device_commands WHERE id=? AND device_id=?", (command_id, dev["device_id"])).fetchone()
    if not row:
        raise CommandError("Unknown command id for this device.", 404)
    db.execute("UPDATE device_commands SET status=?, ack_at=?, message=? WHERE id=?", (status, ts(), message, command_id))
    if row["command"] == "START_CLEANING" and dev["job_id"] is not None:
        if status == "failed":
            fail_job(db, dev, f"Device reported cleaning failure: {message or 'no detail'}")
        elif status == "completed" and dev["job_phase"] == "cleaning":
            db.execute("UPDATE hardware_devices SET job_phase='verifying', phase_until=?, motor_status='idle' WHERE device_id=?",
                       (time.time() + 2, dev["device_id"]))
    elif status == "failed":
        create_alert(db, "warning", f"Command failed: {row['command']}", message or "", dev["drain_id"], dev["device_id"], dedupe=False)


# ----------------------------------------------------------------- periodic tick (called on API polls)
def _simulate(db, dev, now):
    did = dev["device_id"]
    dt = min(max(now - (dev["last_tick"] or now), 0), 5.0)
    if dt < 0.8:
        return
    db.execute("UPDATE hardware_devices SET last_tick=? WHERE device_id=?", (now, did))
    if dev["sim_offline"]:
        return advance_job(db, did, now)
    waste, water = dev["waste_level"], dev["water_level"]
    running = (dev["job_id"] is not None and dev["job_phase"] == "cleaning" and dev["motor_status"] == "running"
               and not dev["fault"] and not dev["estop"] and not dev["hw_estop"])
    if running:
        waste = max(2.0, waste - config.SIM_CLEAN_RATE * dt)
        water = max(5.0, water - 2.0 * dt)
    elif dev["job_id"] is None and not dev["fault"]:
        waste = clamp(waste + config.SIM_DRIFT * dt * random.uniform(0.5, 1.5))
        water = clamp(15 + waste * 0.65 + random.uniform(-1, 1))
    db.execute("UPDATE hardware_devices SET waste_level=?, water_level=?, last_seen=? WHERE device_id=?", (waste, water, now, did))
    dev = get_dev(db, did)
    if now - (dev["last_logged"] or 0) >= 5:
        record_reading(db, dev, "simulated")
        db.execute("UPDATE hardware_devices SET last_logged=? WHERE device_id=?", (now, did))
    advance_job(db, did, now)
    sync_drain(db, get_dev(db, did))
    maybe_auto_clean(db, did)


def monitor_tick(db):
    """Advance simulated devices, enforce time-outs, track online/offline changes."""
    with _lock:
        now = time.time()
        for row in db.execute("SELECT device_id FROM hardware_devices").fetchall():
            dev = get_dev(db, row["device_id"])
            if dev["device_type"] == "simulated":
                _simulate(db, dev, now)
            else:
                advance_job(db, dev["device_id"], now)
            dev = get_dev(db, row["device_id"])
            online = is_online(dev, now)
            if online != bool(dev["was_online"]):
                db.execute("UPDATE hardware_devices SET was_online=? WHERE device_id=?", (1 if online else 0, dev["device_id"]))
                create_alert(db, "info" if online else "warning",
                             f"Device {'online' if online else 'OFFLINE'}: {dev['device_id']}",
                             "Heartbeat received." if online else f"No heartbeat for over {config.HEARTBEAT_TIMEOUT_S}s.",
                             dev["drain_id"], dev["device_id"], dedupe=False)
        db.commit()
