import os
import json
import re
import time
import shutil
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Any

app = FastAPI()

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

EVENTS_REQUEST_DIR = "events/request"
EVENTS_RESPONSE_DIR = "events/response"
EVENTS_PROCESSED_DIR = "events/processed_response"

# Ensure directories exist
os.makedirs(EVENTS_REQUEST_DIR, exist_ok=True)
os.makedirs(EVENTS_RESPONSE_DIR, exist_ok=True)
os.makedirs(EVENTS_PROCESSED_DIR, exist_ok=True)

class Event(BaseModel):
    type: str
    username: str
    project_name: str
    payload: Optional[Any] = None
    timestamp: Optional[str] = None

def _sanitize_for_filename(s: str) -> str:
    if not s:
        return ""
    # letters, digits, _ . - only
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)

@app.post("/events")
async def send_event(event: Event):
    try:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        filename = f"server_{timestamp}.json"
        filepath = os.path.join(EVENTS_REQUEST_DIR, filename)

        with open(filepath, "w") as f:
            json.dump(event.dict(), f)

        return {"status": "success", "id": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/events")
async def get_events(username: str, project_name: str):
    events = []
    try:
        safe_user = _sanitize_for_filename(username)
        safe_project = _sanitize_for_filename(project_name)
        prefix = f"{safe_user}__{safe_project}__"
        # Read all response files
        files = sorted(os.listdir(EVENTS_RESPONSE_DIR))
        for file in files:
            if file.startswith(prefix):
                filepath = os.path.join(EVENTS_RESPONSE_DIR, file)
                try:
                    with open(filepath, "r") as f:
                        data = json.load(f)
                    events.append(data)
                    os.remove(filepath)

                except Exception as e:
                    print(f"Error reading {file}: {e}")

        return events
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
