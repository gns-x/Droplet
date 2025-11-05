# main.py
# FastAPI Droplet Manager Prototype
# - /            health
# - /register    POST: logs a client-provided event to Airtable
# - /list        GET: lists DO droplets via REST API, logs each to Airtable
# - /power/{id}  POST: control (power_on | power_off | reboot), requires ADMIN_TOKEN via Authorization: Bearer or x_admin_token
# - /destroy/{id} DELETE: destroy droplet, requires ADMIN_TOKEN (use with caution)
#
# Env vars:
#   DO_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_API_KEY, AIRTABLE_TABLE, ADMIN_TOKEN

import os
import time
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

DO_TOKEN = os.getenv("DO_TOKEN")
AT_BASE = os.getenv("AIRTABLE_BASE_ID")
AT_KEY = os.getenv("AIRTABLE_API_KEY")
AT_TABLE = os.getenv("AIRTABLE_TABLE", "events")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

app = FastAPI()


def log_event(
    droplet_id: Optional[int] = None,
    name: Optional[str] = None,
    ip: Optional[str] = None,
    status: str = "ok",
    created: Optional[str] = None,
) -> None:
    if not (AT_BASE and AT_KEY and AT_TABLE):
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
        requests.post(url, headers=headers, json={"fields": fields}, timeout=10)
    except Exception:
        pass


def do_api(method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not DO_TOKEN:
        raise HTTPException(status_code=500, detail="DO_TOKEN missing")
    url = f"https://api.digitalocean.com/v2{path}"
    headers = {"Authorization": f"Bearer {DO_TOKEN}", "Content-Type": "application/json"}
    r = requests.request(method, url, headers=headers, json=json_body, timeout=20)
    r.raise_for_status()
    return r.json() if r.text else {}


def require_admin_auth(authorization: Optional[str], x_admin_token: Optional[str]) -> None:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    elif x_admin_token:
        token = x_admin_token
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/register")
async def register(req: Request) -> JSONResponse:
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

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
        raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(missing)}")

    log_event(droplet_id=droplet_id, name=name, ip=ip, status="registered", created=created)
    return JSONResponse({"ok": True, "received": body})


@app.get("/list")
def list_droplets() -> JSONResponse:
    try:
        data = do_api("GET", "/droplets")
        droplets = data.get("droplets", [])
        results: List[Dict[str, Any]] = []
        for d in droplets:
            droplet_id = d.get("id")
            name = d.get("name")
            status = d.get("status")
            created_at = d.get("created_at")
            ip = None
            try:
                v4_list = d.get("networks", {}).get("v4", [])
                if isinstance(v4_list, list) and v4_list:
                    public_v4 = next((n for n in v4_list if n.get("type") == "public"), None)
                    ip = (public_v4 or v4_list[0]).get("ip_address")
            except Exception:
                ip = None
            row = {"droplet_id": droplet_id, "name": name, "ip": ip, "status": status, "created": created_at}
            results.append(row)
            log_event(droplet_id=droplet_id, name=name, ip=ip or "", status=status or "unknown", created=created_at)
        return JSONResponse({"count": len(results), "droplets": results})
    except requests.HTTPError as e:
        detail = f"DigitalOcean API error: {e.response.status_code} {e.response.text}"
        log_event(status="error", created=time.strftime("%Y-%m-%d %H:%M:%S"))
        raise HTTPException(status_code=500, detail=detail)
    except Exception as e:
        log_event(status="error", created=time.strftime("%Y-%m-%d %H:%M:%S"))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/power/{droplet_id}")
def power_action(
    droplet_id: int,
    action: str,
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None, convert_underscores=False),
) -> JSONResponse:
    require_admin_auth(authorization, x_admin_token)
    if action not in {"power_on", "power_off", "reboot"}:
        raise HTTPException(status_code=400, detail="Invalid action")
    try:
        resp = do_api("POST", f"/droplets/{droplet_id}/actions", {"type": action})
        log_event(droplet_id=droplet_id, status=f"action:{action}")
        return JSONResponse({"ok": True, "action": action, "droplet_id": droplet_id, "response": resp})
    except requests.HTTPError as e:
        detail = f"DigitalOcean API error: {e.response.status_code} {e.response.text}"
        log_event(droplet_id=droplet_id, status="error")
        raise HTTPException(status_code=500, detail=detail)
    except Exception as e:
        log_event(droplet_id=droplet_id, status="error")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/destroy/{droplet_id}")
def destroy(
    droplet_id: int,
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None, convert_underscores=False),
) -> JSONResponse:
    require_admin_auth(authorization, x_admin_token)
    try:
        _ = do_api("DELETE", f"/droplets/{droplet_id}")
        log_event(droplet_id=droplet_id, status="destroyed")
        return JSONResponse({"ok": True, "droplet_id": droplet_id})
    except requests.HTTPError as e:
        detail = f"DigitalOcean API error: {e.response.status_code} {e.response.text}"
        log_event(droplet_id=droplet_id, status="error")
        raise HTTPException(status_code=500, detail=detail)
    except Exception as e:
        log_event(droplet_id=droplet_id, status="error")
        raise HTTPException(status_code=500, detail=str(e))

