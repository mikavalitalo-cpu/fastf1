from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

from datetime import datetime

@app.get("/positions")
def positions():
    return {
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "positions": [
            {"position": 1, "driver": "VER", "name": "Max Verstappen"},
            {"position": 2, "driver": "NOR", "name": "Lando Norris"},
            {"position": 3, "driver": "LEC", "name": "Charles Leclerc"},
        ]
    }
