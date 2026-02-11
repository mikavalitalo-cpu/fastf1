import os
import time
import random
import hmac
import threading
from typing import List, Dict, Any, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# -----------------------------
# Config (Render environment variables)
# -----------------------------
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
# Prefer service role key for server-side access (recommended)
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
# If you only have anon key, you can use this instead (less ideal server-side)
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

SIM_RACE_ID = "test-race-simulation-gp"
TICK_SECONDS = 5.0

# Optional: allow your Lovable domain(s). "*" is OK for hobby project.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# In-memory simulation state
# Note: this resets whenever Render restarts/sleeps cold.
# -----------------------------
_state_lock = threading.Lock()
_sim_on: bool = False
_grid: List[str] = []  # full driver order
_last_tick_monotonic: float = 0.0
_tick_count: int = 0
_last_grid_loaded_at_utc: Optional[str] = None


# -----------------------------
# Helpers
# -----------------------------
def utc_iso_now() -> str:
    # ISO-like without importing datetime repeatedly (simple enough for UI)
    # If you want strict ISO with timezone: use datetime.now(timezone.utc).isoformat()
    import datetime
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _get_supabase_key() -> str:
    # Service role key is preferred on backend.
    if SUPABASE_SERVICE_ROLE_KEY:
        return SUPABASE_SERVICE_ROLE_KEY
    if SUPABASE_ANON_KEY:
        return SUPABASE_ANON_KEY
    return ""


def _require_admin_token(x_admin_token: Optional[str]) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not configured on server")

    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token")

    # constant-time compare
    if not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid admin token")


def fetch_active_driver_codes_from_supabase() -> List[str]:
    """
    Fetch driver codes from Supabase PostgREST:
    table: drivers
    columns: code, active, created_at
    filter: active = true
    order: created_at asc
    """
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL not configured")
    key = _get_supabase_key()
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY (preferred) or SUPABASE_ANON_KEY not configured")

    url = f"{SUPABASE_URL}/rest/v1/drivers"
    params = {
        "select": "code,created_at",
        "active": "eq.true",
        "order": "created_at.asc",
    }
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }

    resp = requests.get(url, params=params, headers=headers, timeout=10)
    if resp.status_code >= 400:
        raise RuntimeError(f"Supabase drivers fetch failed: {resp.status_code} {resp.text}")

    rows = resp.json()
    codes: List[str] = []
    seen = set()
    for r in rows:
        c = (r.get("code") or "").strip().upper()
        if c and c not in seen:
            seen.add(c)
            codes.append(c)

    return codes


def ensure_grid_loaded(force_reload: bool = False) -> None:
    global _grid, _last_grid_loaded_at_utc

    with _state_lock:
        need = force_reload or (len(_grid) < 8)
    if not need:
        return

    codes = fetch_active_driver_codes_from_supabase()
    if len(codes) < 8:
        raise RuntimeError(f"Not enough active drivers in Supabase (need >= 8, got {len(codes)})")

    with _state_lock:
        _grid = codes
        _last_grid_loaded_at_utc = utc_iso_now()


def perform_lazy_tick_if_needed() -> None:
    """
    Update grid only when sim is on AND at least TICK_SECONDS passed since last tick.
    Make 0–2 adjacent swaps, biased away from P1 so the leader doesn't change every tick.
    """
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

    # do tick outside lock, then lock to apply safely
    # choose 0–2 swaps
    swaps = random.choices([0, 1, 2], weights=[0.25, 0.55, 0.20], k=1)[0]

    with _state_lock:
        n = len(_grid)
        if n < 2:
            return

        for _ in range(swaps):
            # Bias swaps towards middle/back; avoid always messing with P1/P2
            # Choose an index i to swap with i+1
            # 0-based positions: 0..n-1
            # We'll bias i toward 3..n-2 (i+1 must exist)
            if n <= 3:
                i = random.randint(0, n - 2)
            else:
                # Heavier weight for mid-pack:
                candidate_indices = list(range(1, n - 1))  # i in [1..n-2]
                weights = []
                for idx in candidate_indices:
                    # idx close to middle -> higher weight, idx near 1 -> lower
                    mid = (n - 1) / 2.0
                    dist = abs(idx - mid)
                    w = max(0.2, 1.5 - (dist / mid))  # simple bell-ish curve
                    # Slightly reduce changes at very front:
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
    """
    If sim is on -> returns simulated live order for test race.
    If sim is off -> not_live.
    """
    # If sim is off, no need to load drivers or tick.
    with _state_lock:
        sim_on = _sim_on

    if not sim_on:
        return {
            "status": "not_live",
            "race_id": None,
            "updated_at": utc_iso_now(),
            "order": [],
        }

    # Ensure we have a grid (22 drivers from Supabase)
    try:
        ensure_grid_loaded(force_reload=False)
    except Exception as e:
        # If drivers aren't available, treat as error but keep API stable
        return {
            "status": "error",
            "race_id": SIM_RACE_ID,
            "updated_at": utc_iso_now(),
            "order": [],
            "error": str(e),
        }

    # Update order lazily (every ~5 seconds)
    perform_lazy_tick_if_needed()

    return {
        "status": "live",
        "race_id": SIM_RACE_ID,
        "updated_at": utc_iso_now(),
        "order": top8_order_payload(),
    }


# -----------------------------
# Admin simulation endpoints (token-protected)
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

    # Load drivers if needed
    try:
        ensure_grid_loaded(force_reload=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load drivers from Supabase: {e}")

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
    global _grid, _tick_count, _last_tick_monotonic
    _require_admin_token(x_admin_token)

    try:
        ensure_grid_loaded(force_reload=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reload drivers from Supabase: {e}")

    with _state_lock:
        _tick_count = 0
        _last_tick_monotonic = 0.0

    return {"ok": True, "sim_on": _sim_on, "grid_size": len(_grid), "updated_at": utc_iso_now()}

