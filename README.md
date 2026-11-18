# DrainGuard 2.0 — Smart Drain Monitoring & Autonomous Cleaning (Chennai)

A hackathon-ready web app: interactive Chennai drain map, citizen blockage reporting with image upload,
a hardware control center with a built-in **simulated** cleaning machine, alerts, history and an admin area.

> **DEMO MODE:** the 3 drains are *sample placeholders* (approximate neighbourhood points, **not** real drains),
> sensor readings and the motor are **simulated**, and image-analysis results are **simulated** and labelled as such.
> Nothing in demo mode is a real-world measurement.

## 1. Run it on Windows (VS Code)

1. Install **Python 3.10+** from python.org (tick *"Add Python to PATH"*).
2. Open this folder in VS Code (`File → Open Folder…`), then open the terminal (`Ctrl+` ` ).
3. Run these commands one by one:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
If PowerShell blocks `activate`, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

4. Open **http://127.0.0.1:5000** in your browser. The database is created automatically on first run.

**Admin login:** `admin` / `admin123` (needed to add drains, manage reports, control hardware).
Change it before showing others: `set DRAINGUARD_ADMIN_PASSWORD=MyNewPass` (and `DRAINGUARD_SECRET=some-long-random-text`) **before first run**, or delete `instance/drainguard.db` and run `python init_db.py --reset`.

The map and charts load Leaflet/Chart.js/OpenStreetMap from the internet, so you need to be online.

## 2. Two-minute demo script

1. **Dashboard** – counters, live drain cards, charts, map preview.
2. **Hardware Control Center** → log in → pick `SIM-002` → *Simulation tools → Add blockage* → watch the state turn **Blocked**
   → *Start automatic cleaning* → mechanism cycles, waste drops, verification, **success** logged.
3. Press *Inject motor fault* during a cycle → motor stops, **Human assistance** alert, controls locked until *Reset*.
4. `SIM-001` has **Automatic mode ON**: add a blockage and it cleans by itself. After a few cycles the basket fills and cleaning is refused until *Basket emptied*.
5. **Report a Blocked Drain** → upload any PNG/JPG → submit → report ID + **simulated** AI result → **Administration** → change status → it shows on the map (tick *Show citizen reports*).

## 3. Project structure

```
app.py               Flask routes + validation (start here)
config.py            thresholds, limits, admin credentials, DEMO_MODE
db.py                SQLite schema, seed (demo) data, alerts helper
hardware.py          device layer: simulator, cleaning state machine, safety rules, HTTP device API logic
ai_analysis.py       image-analysis interface (simulated until a model is registered)
init_db.py           create / reset the database
templates/           one HTML page per menu item (+ base.html, login.html)
static/css/style.css navy/white theme, responsive
static/js/           common.js + one script per page
sample_data/         CSV template for importing verified drains
tools/               smoke_test.py (backend tests) and device_client_demo.py (fake ESP32)
hardware_examples/   esp32_http_client.ino (reference sketch, untested on hardware)
uploads/             uploaded images (random server-generated filenames)
instance/            drainguard.db (created automatically)
```

Database tables: `drains, citizen_reports, uploaded_images, hardware_devices, sensor_readings, cleaning_history, alerts, users`
(+ `device_commands` for the command queue/acknowledgements).

## 4. API summary

| Method & path | Purpose | Auth |
|---|---|---|
| GET `/api/drains` | list drains (`?status=&q=`) | – |
| POST `/api/drains` | add drain (validated, must be inside Chennai bounds) | admin |
| POST `/api/drains/import` | import verified drains from CSV | admin |
| GET `/api/reports` · POST `/api/reports` | list / create reports (JSON, with `image_ids`) | – |
| PATCH `/api/reports/<id>` | status: Pending → Verified → Cleaning Assigned → Resolved | admin |
| POST `/api/upload` | upload one image (PNG/JPG/WEBP ≤ 5 MB, content checked) → AI result | – |
| GET `/api/hardware` | devices + live state | – |
| POST `/api/hardware/command` | `start_auto_clean`, `start_manual_clean` (+`confirm:true`), `emergency_stop`, `reset_device`, `set_auto_mode` | admin (e-stop: anyone) |
| POST `/api/hardware/register` | register a real device, returns its key once | admin |
| POST `/api/sensor-data` | device → server readings | `X-Device-Key` |
| GET `/api/device/<id>/commands` · POST `.../ack` · POST `.../heartbeat` | device polls commands, acknowledges, heartbeats | `X-Device-Key` |
| GET `/api/history` · GET `/api/alerts` · GET `/api/readings` · GET `/api/dashboard` | data for pages | – |

## 5. Cleaning logic and safety rules

| State | Rule (edit in `config.py`) |
|---|---|
| Normal | waste < 40 % |
| Warning | waste ≥ 40 % |
| Blocked | waste ≥ 70 % → automatic cleaning may start |
| Human assistance | waste ≥ 95 % **or** any fault |

Cleaning cycle: check readings → check device online and safe → start → motor status → (simulation only) waste drops →
verify (waste ≤ 30 %) → record in `cleaning_history`. Any failure stops the motor, latches a fault and raises a critical alert.

Safety: refuses to run if the device is offline, faulted, e-stopped (software or physical) or the basket is full ·
30 s motor time-out · manual start needs confirmation (enforced by the server too) · faults stay latched until a human presses Reset ·
**software can never clear a physical e-stop** · *Emergency stop works without login on purpose* (it can only make things safer).
**Simulation is not real motor control.** A real build needs its own hardware e-stop, current limiting and interlocks.

## 6. Connecting real hardware (HTTP)

1. Administration → *Register a hardware device* → copy the key (shown once).
2. No hardware yet? Test the real-device path with the fake client: `python tools/device_client_demo.py --device MY-ID --key KEY`
3. For an ESP32 see `hardware_examples/esp32_http_client.ino` (reference only, not tested on hardware; use your PC's LAN IP).
   MQTT is not implemented; the same four messages (sensor data, heartbeat, command, ack) map directly onto MQTT topics if you prefer it.

## 7. Plugging in a real AI model

Write a function `path -> {"categories":[{"label","confidence"}], "severity":"Low|Medium|High", "confidence":0.9}` and call
`ai_analysis.register_model(fn)` at start-up (a YOLO example is in the `ai_analysis.py` docstring). Results then stop being labelled *simulated*.

## 8. Real Chennai drain data

Do **not** type guessed coordinates. Use the map's *Add a drain* panel with surveyed points, or import a CSV from an approved source
(`sample_data/drain_import_template.csv`). Imported/added drains are marked as non-demo.

## 9. Tests

`python tools/smoke_test.py` runs 60+ backend checks on a temporary database (pages, validation, uploads, reports, safety paths, real-device flow).

## 10. Known limits (prototype)

Single-process Flask dev server and SQLite; one admin account; simulation advances while pages poll the API; no email/SMS notifications.
