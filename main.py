# main.py
# FastAPI prototype for DigitalOcean droplet manager sprint
# - /register: accepts JSON, validates, logs to Airtable
# - /list: fetches droplets via DigitalOcean REST API, logs each to Airtable
# - /: health endpoint
# Environment: DO_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_API_KEY, AIRTABLE_TABLE

import os
import time
import json
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

DO_TOKEN = os.getenv("DO_TOKEN")
AT_BASE = os.getenv("AIRTABLE_BASE_ID")
AT_KEY = os.getenv("AIRTABLE_API_KEY")
AT_TABLE = os.getenv("AIRTABLE_TABLE", "events")

app = FastAPI()


def log_event(
    droplet_id: Optional[int] = None,
    name: Optional[str] = None,
    ip: Optional[str] = None,
    status: str = "ok",
    created: Optional[str] = None,
) -> None:
    """
    Log a single event row to Airtable.
    Columns expected in Airtable: droplet_id, name, ip, status, created
    """
    if not (AT_BASE and AT_KEY and AT_TABLE):
        # Skip logging if Airtable env vars are missing
        return

    url = f"https://api.airtable.com/v0/{AT_BASE}/{AT_TABLE}"
    headers = {
        "Authorization": f"Bearer {AT_KEY}",
        "Content-Type": "application/json",
    }
    fields: Dict[str, Any] = {
        "droplet_id": droplet_id if droplet_id is not None else "",
        "name": name or "",
        "ip": ip or "",
        "status": status,
        "created": created or time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        # We ignore response errors to avoid breaking the API path; keep sprint simple
        requests.post(url, headers=headers, json={"fields": fields}, timeout=8)
    except Exception:
        # Intentionally swallow to keep endpoints resilient
        pass


@app.get("/")
def health() -> Dict[str, str]:
    # Simple health endpoint
    return {"status": "ok"}


@app.post("/register")
async def register(req: Request) -> JSONResponse:
    """
    Accepts JSON: { droplet_id: int, name: str, ip: str, created?: str }
    Validates required fields, logs to Airtable, returns ok.
    """
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Basic validation
    droplet_id = body.get("droplet_id")
    name = body.get("name")
    ip = body.get("ip")
    created = body.get("created")

    missing: List[str] = []
    if droplet_id is None:
        missing.append("droplet_id")
    if not name:
        missing.append("name")
    if not ip:
        missing.append("ip")

    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required fields: {', '.join(missing)}",
        )

    log_event(droplet_id=droplet_id, name=name, ip=ip, status="registered", created=created)
    return JSONResponse({"ok": True, "received": body})


@app.get("/list")
def list_droplets() -> JSONResponse:
    """
    Lists droplets via DigitalOcean REST API.
    Logs each droplet to Airtable with: droplet_id, name, ip, status, created.
    """
    if not DO_TOKEN:
        raise HTTPException(status_code=500, detail="DO_TOKEN missing")

    try:
        r = requests.get(
            "https://api.digitalocean.com/v2/droplets",
            headers={"Authorization": f"Bearer {DO_TOKEN}"},
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
        droplets = payload.get("droplets", [])

        results: List[Dict[str, Any]] = []
        for d in droplets:
            droplet_id = d.get("id")
            name = d.get("name")
            status = d.get("status")
            created_at = d.get("created_at")

            # Extract public IPv4 if present
            ip = None
            try:
                v4_list = d.get("networks", {}).get("v4", [])
                if isinstance(v4_list, list) and len(v4_list) > 0:
                    # Prefer public IP
                    public_v4 = next((n for n in v4_list if n.get("type") == "public"), None)
                    ip = (public_v4 or v4_list[0]).get("ip_address")
            except Exception:
                ip = None

            row = {
                "droplet_id": droplet_id,
                "name": name,
                "ip": ip,
                "status": status,
                "created": created_at,
            }
            results.append(row)

            # Log each row to Airtable
            log_event(
                droplet_id=droplet_id,
                name=name,
                ip=ip or "",
                status=status or "unknown",
                created=created_at,
            )

        return JSONResponse({"count": len(results), "droplets": results})
    except requests.HTTPError as e:
        # Bubble up API error message
        detail = f"DigitalOcean API error: {e.response.status_code} {e.response.text}"
        log_event(status="error", created=time.strftime("%Y-%m-%d %H:%M:%S"))
        raise HTTPException(status_code=500, detail=detail)
    except Exception as e:
        log_event(status="error", created=time.strftime("%Y-%m-%d %H:%M:%S"))
        raise HTTPException(status_code=500, detail=str(e))

