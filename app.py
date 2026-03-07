import os
import time
import random
import hmac
import threading
from typing import List, Dict, Any, Optional, Tuple

import fastf1
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# --------------------------------------------------
# Config
# --------------------------------------------------
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

# Optional but recommended: define cache location in Render env
# FASTF1_CACHE=/tmp/fastf1-cache
FASTF1_CACHE = os.getenv("FASTF1_CACHE", "").strip()
if FASTF1_CACHE:
    try:
        fastf1.Cache.enable_cache(FASTF1_CACHE)
    except Exception:
        # If cache is already enabled or path fails, do not crash app startup
        pass

# --------------------------------------------------
# Driver code mapping
# Your app codes:
# PER,GAS,COL,ALO,STR,BOT,LEC,HAM,BEA,OCO,PIA,NOR,RUS,LAW,TSU,VER,HUL,BOR,SAI,ALB,ANT
#
# FastF1 usually uses official abbreviations, but rookies / changed seats may differ.
# Adjust these aliases if needed after the first real race weekend test.
# --------------------------------------------------
FASTF1_TO_APP_CODE = {
    # Identity/common
    "PER": "PER",
    "GAS": "GAS",
    "COL": "COL",
    "ALO": "ALO",
    "STR": "STR",
    "BOT": "BOT",
    "LEC": "LEC",
    "HAM": "HAM",
    "BEA": "BEA",
    "OCO": "OCO",
    "PIA": "PIA",
    "NOR": "NOR",
    "RUS": "RUS",
    "LAW": "LAW",
    "VER": "VER",
    "HUL": "HUL",
    "SAI": "SAI",
    "ALB": "ALB",
    "ANT": "ANT",
    "HAD": "HAD",   
    "LIN": "LIN",   
    "BOR": "BOR",
}

# --------------------------------------------------
# 2026 race_id -> round mapping
# Add aliases so backend survives small naming differences in DB seeds.
# --------------------------------------------------
RACE_ID_TO_ROUND = {
    "2026-australian": 1,
    "2026-australia": 1,
    "2026-chinese": 2,
    "2026-japanese": 3,
    "2026-bahrain": 4,
    "2026-saudi-arabian": 5,
    "2026-miami": 6,
    "2026-canadian": 7,
    "2026-monaco": 8,
    "2026-catalunya": 9,
    "2026-barcelona": 9,
    "2026-austrian": 10,
    "2026-british": 11,
    "2026-belgian": 12,
    "2026-hungarian": 13,
    "2026-dutch": 14,
    "2026-italian": 15,
    "2026-spanish": 16,
    "2026-spanish-madrid": 16,
    "2026-azerbaijan": 17,
    "2026-singapore": 18,
    "2026-united-states": 19,
    "2026-mexico": 20,
    "2026-mexican": 20,
    "2026-brazilian": 21,
    "2026-las-vegas": 22,
    "2026-qatar": 23,
    "2026-abu-dhabi": 24,
    SIM_RACE_ID: 0,
}

# --------------------------------------------------
# In-memory simulation state
# --------------------------------------------------
_state_lock = threading.Lock()
_sim_on: bool = False
_grid: List[str] = []
_last_tick_monotonic: float = 0.0
_tick_count: int = 0
_last_grid_loaded_at_utc: Optional[str] = None


# --------------------------------------------------
# Helpers
# --------------------------------------------------
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


def normalize_driver_code(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    code = str(code).strip().upper()
    return FASTF1_TO_APP_CODE.get(code, code)


def parse_driver_codes_from_env() -> List[str]:
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
        "RUS", "LAW", "LIN", "VER", "HAD", "HUL",
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


def get_round_for_race_id(race_id: str) -> int:
    rid = (race_id or "").strip()
    if rid not in RACE_ID_TO_ROUND:
        raise ValueError(f"Unknown race_id: {rid}")
    return RACE_ID_TO_ROUND[rid]


def load_fastf1_session_for_race_id(race_id: str, identifier: str):
    round_no = get_round_for_race_id(race_id)
    if round_no == 0:
        raise ValueError("Test race does not use FastF1 sessions")
    session = fastf1.get_session(2026, round_no, identifier)
    session.load(laps=False, telemetry=False, weather=False)
    return session


def safe_int(value) -> Optional[int]:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            if not value.isdigit():
                return None
        return int(float(value))
    except Exception:
        return None


def extract_grid_from_race_results(results_df) -> List[Dict[str, Any]]:
    rows = []
    if results_df is None or len(results_df) == 0:
        return rows

    seen_pos = set()
    seen_drv = set()

    for _, row in results_df.iterrows():
        driver = str(row.get("Abbreviation")).strip().upper()
        pos = safe_int(row.get("GridPosition"))

        if driver and pos and pos > 0 and driver not in seen_drv and pos not in seen_pos:
            rows.append({"position": pos, "driver": driver})
            seen_drv.add(driver)
            seen_pos.add(pos)

    rows.sort(key=lambda x: x["position"])

    # --------------------------------------------------
    # Fill missing drivers if FastF1 grid incomplete
    # --------------------------------------------------

    expected = parse_driver_codes_from_env()

    missing = [d for d in expected if d not in seen_drv]

    next_pos = len(rows) + 1

    for drv in missing:
        rows.append({
            "position": next_pos,
            "driver": drv
        })
        next_pos += 1

    return rows


def extract_grid_from_quali_results(results_df) -> List[Dict[str, Any]]:
    rows = []
    if results_df is None or len(results_df) == 0:
        return rows

    seen_pos = set()
    seen_drv = set()

    for _, row in results_df.iterrows():
        driver = str(row.get("Abbreviation")).strip().upper()
        pos = safe_int(row.get("Position"))

        if driver and pos and pos > 0 and driver not in seen_drv and pos not in seen_pos:
            rows.append({"position": pos, "driver": driver})
            seen_drv.add(driver)
            seen_pos.add(pos)

    rows.sort(key=lambda x: x["position"])

    # --------------------------------------------------
    # Ensure we always return full grid of drivers
    # --------------------------------------------------

    expected = parse_driver_codes_from_env()

    missing = [d for d in expected if d not in seen_drv]

    next_pos = len(rows) + 1

    for drv in missing:
        rows.append({
            "position": next_pos,
            "driver": drv
        })
        next_pos += 1

    return rows


def extract_race_results(results_df) -> List[Dict[str, Any]]:
    """
    Returns only drivers with numeric classification.
    DNF/DQ/etc. are intentionally excluded from numeric results.
    """
    rows = []
    if results_df is None or len(results_df) == 0:
        return rows

    seen_pos = set()
    seen_drv = set()

    for _, row in results_df.iterrows():
        driver = normalize_driver_code(row.get("Abbreviation"))

        # Prefer Position, fallback to ClassifiedPosition if numeric
        pos = safe_int(row.get("Position"))
        if pos is None:
            pos = safe_int(row.get("ClassifiedPosition"))

        if driver and pos and pos > 0 and driver not in seen_drv and pos not in seen_pos:
            rows.append({"position": pos, "driver": driver})
            seen_drv.add(driver)
            seen_pos.add(pos)

    rows.sort(key=lambda x: x["position"])
    return rows


def infer_results_provisional(results_df, numeric_results: List[Dict[str, Any]]) -> bool:
    """
    Conservative rule:
    - If there are no results -> provisional/unfinished by definition
    - If many drivers have a non-numeric ClassifiedPosition or data is sparse -> provisional
    - Otherwise still default to provisional=True, because admin approval is the final gate
    """
    if results_df is None or len(results_df) == 0 or len(numeric_results) == 0:
        return True

    # Conservative default for race-day operations.
    return True


# --------------------------------------------------
# Public endpoints
# --------------------------------------------------
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


@app.get("/grid")
def grid(race_id: str = Query(..., description="e.g. 2026-australian")):
    if race_id == SIM_RACE_ID:
        # For test race, use current simulated/default grid order
        try:
            ensure_grid_loaded(force_reload=False)
            with _state_lock:
                rows = [{"position": i + 1, "driver": d} for i, d in enumerate(_grid)]
            return {
                "status": "ok",
                "grid_status": "final",
                "race_id": race_id,
                "updated_at": utc_iso_now(),
                "grid": rows,
            }
        except Exception as e:
            return {
                "status": "error",
                "grid_status": "provisional",
                "race_id": race_id,
                "updated_at": utc_iso_now(),
                "grid": [],
                "error": str(e),
            }

    try:
        # 1) Try Race session GridPosition
        race_session = load_fastf1_session_for_race_id(race_id, "R")
        race_results = getattr(race_session, "results", None)
        race_grid = extract_grid_from_race_results(race_results)

        if len(race_grid) > 0:
            return {
                "status": "ok",
                "grid_status": "final",
                "race_id": race_id,
                "updated_at": utc_iso_now(),
                "grid": race_grid,
            }

        # 2) Fallback to Qualifying order
        quali_session = load_fastf1_session_for_race_id(race_id, "Q")
        quali_results = getattr(quali_session, "results", None)
        quali_grid = extract_grid_from_quali_results(quali_results)

        if len(quali_grid) > 0:
            return {
                "status": "ok",
                "grid_status": "provisional",
                "race_id": race_id,
                "updated_at": utc_iso_now(),
                "grid": quali_grid,
            }

        return {
            "status": "not_available",
            "grid_status": "provisional",
            "race_id": race_id,
            "updated_at": utc_iso_now(),
            "grid": [],
        }

    except Exception as e:
        return {
            "status": "error",
            "grid_status": "provisional",
            "race_id": race_id,
            "updated_at": utc_iso_now(),
            "grid": [],
            "error": str(e),
        }


@app.get("/results")
def results(race_id: str = Query(..., description="e.g. 2026-australian")):
    if race_id == SIM_RACE_ID:
        # Sim mode: if sim on, current order can be treated as current result preview,
        # but not an official finished result.
        with _state_lock:
            sim_on = _sim_on
        if sim_on:
            return {
                "status": "not_finished",
                "race_id": race_id,
                "updated_at": utc_iso_now(),
                "is_provisional": True,
                "results": [],
            }
        else:
            return {
                "status": "not_available",
                "race_id": race_id,
                "updated_at": utc_iso_now(),
                "is_provisional": True,
                "results": [],
            }

    try:
        race_session = load_fastf1_session_for_race_id(race_id, "R")
        race_results_df = getattr(race_session, "results", None)
        numeric_results = extract_race_results(race_results_df)

        if len(numeric_results) == 0:
            return {
                "status": "not_finished",
                "race_id": race_id,
                "updated_at": utc_iso_now(),
                "is_provisional": True,
                "results": [],
            }

        is_provisional = infer_results_provisional(race_results_df, numeric_results)

        return {
            "status": "finished",
            "race_id": race_id,
            "updated_at": utc_iso_now(),
            "is_provisional": is_provisional,
            "results": numeric_results,
        }

    except Exception as e:
        return {
            "status": "error",
            "race_id": race_id,
            "updated_at": utc_iso_now(),
            "is_provisional": True,
            "results": [],
            "error": str(e),
        }


# --------------------------------------------------
# Admin simulation endpoints
# --------------------------------------------------
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
