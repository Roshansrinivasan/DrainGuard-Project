"""DrainGuard 2.0 - central configuration. Change values here (or via environment variables)."""
import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "drainguard.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")

DEMO_MODE = True                      # shows the "simulated data" banner in the UI
SECRET_KEY = os.environ.get("DRAINGUARD_SECRET", "change-me-before-any-real-deployment")
ADMIN_USER = os.environ.get("DRAINGUARD_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("DRAINGUARD_ADMIN_PASSWORD", "admin123")   # CHANGE THIS

# ---- uploads ----
MAX_UPLOAD_MB = 5
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_IMAGES_PER_REPORT = 3

# ---- map validation: coordinates must fall roughly inside the Chennai region ----
LAT_RANGE = (12.75, 13.35)
LNG_RANGE = (79.95, 80.40)

# ---- drain status thresholds (waste level, %) ----
WARNING_WASTE = 40       # >= warning
BLOCKED_WASTE = 70       # >= blocked (automatic cleaning may start)
CRITICAL_WASTE = 95      # >= human assistance required, automation refuses to run
CLEAN_TARGET = 15        # cleaning cycle ends when waste <= this
VERIFY_MAX_WASTE = 30    # after cleaning, waste must be <= this to count as success
BASKET_FULL = 90         # basket %: automatic cleaning refused above this

# ---- safety timing ----
MOTOR_TIMEOUT_S = 30         # motor may not run longer than this per cycle
HEARTBEAT_TIMEOUT_S = 20     # device is "offline" if silent for this long

# ---- simulated device behaviour (demo only) ----
SIM_DRIFT = 0.5              # % waste added per second (slow natural build-up)
SIM_CLEAN_RATE = 6.0         # % waste removed per second while the simulated motor runs
