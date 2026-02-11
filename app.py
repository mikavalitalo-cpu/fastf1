import os
import time
import random
import hmac
import threading
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# -----------------------------
# Config (Render environment variables)
# -----------------------------
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DRIVER_CODES_ENV = os.getenv("DRIVER_CODES", "").strip()

SIM_RACE_ID = "test-race-simulation-gp"
TICK_SECONDS = 5.0

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# In-memory simulation state
# -----------------------------
_state_lock = threading.Lock()
_sim_on: bool = False
_grid: List[str] = []
_last_tick_monotonic: float = 0.0
_tick_count: int = 0
_last_grid_loaded_at_utc: Optional[str] = None


# -----------------------------
# Helpers
# -----------------------------
def utc_iso_now() -> str:
    import datetime
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _require_admin_token(x_admin_token: Optional[str]) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not configured on server")

    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token")

    if not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid admin token")


def parse_driver_codes_from_env() -> List[str]:
    """
    Expect 22 driver codes comma-separated in DRIVER_CODES env var.
    If not provided, fallback to correct 2026 22-driver list.
    """
    if DRIVER_CODES_ENV:
        parts = [p.strip().upper() for p in DRIVER_CODES_ENV.split(",")]
        codes: List[str] = []
        seen = set()
        for c in parts:
            if c and c not in seen:
                seen.add(c)
                codes.append(c)
        return codes

    # Fallback: your actual 22-driver list
    return [
        "PER", "GAS", "COL", "ALO", "STR", "BOT",
        "LEC", "HAM", "BEA", "OCO", "PIA", "NOR",
        "RUS", "LAW", "TSU", "VER", "ISA", "HUL",
        "BOR", "SAI", "ALB", "ANT",
    ]


def ensure_grid_loaded(force_reload: bool = False) -> None:
    global _grid, _last_grid_loaded_at_utc

    with _state_lock:
        need = force_reload or (len(_grid) < 8)
    if not need:
        return

    codes = parse_driver_codes_from_env()
    if len(codes) < 8:
        raise RuntimeError(f"Not enough driver codes (need >= 8, got {len(codes)})")

    with _state_lock:
        _grid = codes.copy()
        _last_grid_loaded_at_utc = utc_iso_now()


def perform_lazy_tick_if_needed() -> None:
    global _last_tick_monotonic, _tick_count

    with _state_lock:
        if not _sim_on:
            return

        now_m = time.monotonic()
        if _last_tick_monotonic == 0.0:
            _last_tick_monotonic = now_m
            return

        if (now_m - _last_tick_monotonic) < TICK_SECONDS:
            return

    swaps = random.choices([0, 1, 2], weights=[0.25, 0.55, 0.20], k=1)[0]

    with _state_lock:
        n = len(_grid)
        if n < 2:
            return

        for _ in range(swaps):
            if n <= 3:
                i = random.randint(0, n - 2)
            else:
                candidate_indices = list(range(1, n - 1))
                weights = []
                for idx in candidate_indices:
                    mid = (n - 1) / 2.0
                    dist = abs(idx - mid)
                    w = max(0.2, 1.5 - (dist / mid))
                    if idx <= 1:
                        w *= 0.5
                    weights.append(w)
                i = random.choices(candidate_indices, weights=weights, k=1)[0]
                i = min(i, n - 2)

            _grid[i], _grid[i + 1] = _grid[i + 1], _grid[i]

        _tick_count += 1
        _last_tick_monotonic = time.monotonic()


def top8_order_payload() -> List[Dict[str, Any]]:
    with _state_lock:
        top = _grid[:8]
    return [{"position": i + 1, "driver": code} for i, code in enumerate(top)]


# -----------------------------
# Public endpoints
# -----------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/positions")
def positions():
    with _state_lock:
        sim_on = _sim_on

    if not sim_on:
        return {
            "status": "not_live",
            "race_id": None,
            "updated_at": utc_iso_now(),
            "order": [],
        }

    try:
        ensure_grid_loaded(force_reload=False)
    except Exception as e:
        return {
            "status": "error",
            "race_id": SIM_RACE_ID,
            "updated_at": utc_iso_now(),
            "order": [],
            "error": str(e),
        }

    perform_lazy_tick_if_needed()

    return {
        "status": "live",
        "race_id": SIM_RACE_ID,
        "updated_at": utc_iso_now(),
        "order": top8_order_payload(),
    }


# -----------------------------
# Admin simulation endpoints
# -----------------------------
@app.get("/sim/status")
def sim_status(x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    _require_admin_token(x_admin_token)
    with _state_lock:
        return {
            "sim_on": _sim_on,
            "tick_count": _tick_count,
            "grid_size": len(_grid),
            "last_grid_loaded_at": _last_grid_loaded_at_utc,
            "updated_at": utc_iso_now(),
        }


@app.post("/sim/start")
def sim_start(x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    global _sim_on
    _require_admin_token(x_admin_token)

    ensure_grid_loaded(force_reload=True)

    with _state_lock:
        _sim_on = True

    return {"ok": True, "sim_on": True, "race_id": SIM_RACE_ID, "updated_at": utc_iso_now()}


@app.post("/sim/stop")
def sim_stop(x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    global _sim_on
    _require_admin_token(x_admin_token)
    with _state_lock:
        _sim_on = False
    return {"ok": True, "sim_on": False, "updated_at": utc_iso_now()}


@app.post("/sim/reset")
def sim_reset(x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    global _tick_count, _last_tick_monotonic
    _require_admin_token(x_admin_token)

    ensure_grid_loaded(force_reload=True)

    with _state_lock:
        _tick_count = 0
        _last_tick_monotonic = 0.0

    return {"ok": True, "sim_on": _sim_on, "grid_size": len(_grid), "updated_at": utc_iso_now()}
