from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/positions")
def positions():
    # Fake data for testing the frontend
    return [
        {"position": 1, "driver": "VER", "name": "Max Verstappen"},
        {"position": 2, "driver": "NOR", "name": "Lando Norris"},
        {"position": 3, "driver": "LEC", "name": "Charles Leclerc"},
        {"position": 4, "driver": "HAM", "name": "Lewis Hamilton"},
        {"position": 5, "driver": "SAI", "name": "Carlos Sainz"},
    ]
