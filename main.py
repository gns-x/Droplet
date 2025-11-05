# main.py
import os
import time
from typing import Optional, Dict, Any, List
import requests
from fastapi import FastAPI, Request, HTTPException, Header, Form
from fastapi.responses import JSONResponse, HTMLResponse
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
    assigned_to: Optional[str] = None,
) -> None:
    if not (AT_BASE and AT_KEY and AT_TABLE):
        return
    url = f"https://api.airtable.com/v0/{AT_BASE}/{AT_TABLE}"
    headers = {"Authorization": f"Bearer {AT_KEY}", "Content-Type": "application/json"}
    fields: Dict[str, Any] = {
        "droplet_id": droplet_id if droplet_id is not None else "",
        "name": name or "",
        "ip": ip or "",
        "status": status,
        "created": created or time.strftime("%Y-%m-%d %H:%M:%S"),
        "assigned_to": assigned_to or "",
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
    assigned_to = body.get("assigned_to")

    missing: List[str] = []
    if droplet_id is None:
        missing.append("droplet_id")
    if not name:
        missing.append("name")
    if not ip:
        missing.append("ip")
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(missing)}")

    log_event(droplet_id=droplet_id, name=name, ip=ip, status="registered", created=created, assigned_to=assigned_to)
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
            tags = d.get("tags", [])
            assigned_to = next((tag.replace("assigned:", "") for tag in tags if tag.startswith("assigned:")), "")

            ip = None
            try:
                v4_list = d.get("networks", {}).get("v4", [])
                if isinstance(v4_list, list) and v4_list:
                    public_v4 = next((n for n in v4_list if n.get("type") == "public"), None)
                    ip = (public_v4 or v4_list[0]).get("ip_address")
            except Exception:
                ip = None

            row = {"droplet_id": droplet_id, "name": name, "ip": ip, "status": status, "created": created_at, "assigned_to": assigned_to}
            results.append(row)
            log_event(droplet_id=droplet_id, name=name, ip=ip or "", status=status or "unknown", created=created_at, assigned_to=assigned_to)

        return JSONResponse({"count": len(results), "droplets": results})
    except requests.HTTPError as e:
        detail = f"DigitalOcean API error: {e.response.status_code} {e.response.text}"
        log_event(status="error", created=time.strftime("%Y-%m-%d %H:%M:%S"))
        raise HTTPException(status_code=500, detail=detail)
    except Exception as e:
        log_event(status="error", created=time.strftime("%Y-%m-%d %H:%M:%S"))
        raise HTTPException(status_code=500, detail=str(e))


# ---------- Dashboard ----------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    try:
        data = do_api("GET", "/droplets")
        droplets = data.get("droplets", [])
    except Exception as e:
        return html_page("Droplet Dashboard", f"<div class='card error'>Error loading droplets: {e}</div>")

    cards = []
    for d in droplets:
        droplet_id = d.get("id")
        name = d.get("name")
        status = d.get("status")
        created = d.get("created_at")
        tags = d.get("tags", [])
        assigned_to = next((tag.replace("assigned:", "") for tag in tags if tag.startswith("assigned:")), "Unassigned")

        ip = "-"
        v4_list = d.get("networks", {}).get("v4", [])
        if isinstance(v4_list, list) and v4_list:
            public_v4 = next((n for n in v4_list if n.get("type") == "public"), None)
            ip = (public_v4 or v4_list[0]).get("ip_address") or "-"
        badge_cls = "ok" if status == "active" else "warn" if status == "new" else "err"
        cards.append(f"""
        <div class="card">
          <div class="card-head">
            <div class="title">{name}</div>
            <span class="badge {badge_cls}">{status}</span>
          </div>
          <div class="meta">
            <div><span>ID</span><strong>{droplet_id}</strong></div>
            <div><span>IP</span><strong>{ip}</strong></div>
            <div><span>Assigned To</span><strong class="assigned">{assigned_to}</strong></div>
            <div><span>Created</span><strong>{created}</strong></div>
          </div>
          <div class="actions">
            <button onclick="openModal('{droplet_id}','reboot')">Reboot</button>
            <button class="ghost" onclick="openModal('{droplet_id}','power_off')">Power Off</button>
            <button class="ghost" onclick="openModal('{droplet_id}','power_on')">Power On</button>
          </div>
          <div class="edit-actions">
            <button class="edit" onclick="openEditModal('{droplet_id}','{name}','{assigned_to}')">✏️ Edit Name/Assignment</button>
          </div>
        </div>
        """)

    body = f"""
    <div class="header">
      <h1>Droplet Dashboard</h1>
      <p class="sub">Overview of your DigitalOcean droplets with quick controls.</p>
      <div class="toolbar">
        <input id="search" type="text" placeholder="Search by name, IP, or assignment…" oninput="filterCards()"/>
        <span class="count">Total: {len(droplets)}</span>
      </div>
    </div>

    <div id="grid" class="grid">{''.join(cards)}</div>

    <!-- Modals and JS remain the same as previous code -->
    """  # your existing modal HTML and JS go here

    return html_page("Droplet Dashboard", body)


@app.post("/dashboard/edit", response_class=HTMLResponse)
def dashboard_edit(
    droplet_id: int = Form(...),
    name: str = Form(...),
    assigned_to: str = Form(default=""),
    admin_token: str = Form(...),
) -> HTMLResponse:
    if not ADMIN_TOKEN or admin_token.strip() != ADMIN_TOKEN.strip():
        return HTMLResponse("<span class='errtxt'>Unauthorized</span>", status_code=401)

    # Only updating droplet name via DO API; "assigned_to" is UI-only
    try:
        do_api("PUT", f"/droplets/{droplet_id}", json_body={"name": name})
        # Store assigned_to in UI cache or Airtable if needed
    except requests.HTTPError as e:
        return HTMLResponse(f"<span class='errtxt'>DO error: {e.response.status_code} - {e.response.text}</span>", status_code=500)
    except Exception as e:
        return HTMLResponse(f"<span class='errtxt'>Error: {e}</span>", status_code=500)

    return HTMLResponse("<span class='oktxt'>Updated</span>")
    


def html_page(title: str, body: str) -> HTMLResponse:
    """Premium, elegant UI with white background"""
    css = """
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #ffffff;
            min-height: 100vh;
            padding: 2rem;
            color: #1a202c;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: #ffffff;
            border-radius: 16px;
            padding: 3rem;
        }
        
        .header {
            margin-bottom: 3rem;
            padding-bottom: 2rem;
            border-bottom: 2px solid #e2e8f0;
        }
        
        .header h1 {
            font-size: 2.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 0.5rem;
        }
        
        .sub {
            color: #718096;
            font-size: 1.1rem;
            margin-bottom: 1.5rem;
        }
        
        .toolbar {
            display: flex;
            gap: 1rem;
            align-items: center;
            flex-wrap: wrap;
        }
        
        #search {
            flex: 1;
            min-width: 300px;
            padding: 0.875rem 1.25rem;
            border: 2px solid #e2e8f0;
            border-radius: 12px;
            font-size: 1rem;
            transition: all 0.3s ease;
            background: white;
        }
        
        #search:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 4px rgba(102, 126, 234, 0.1);
        }
        
        .count {
            padding: 0.75rem 1.5rem;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border-radius: 12px;
            font-weight: 600;
            font-size: 0.95rem;
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
        }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
            gap: 1.5rem;
        }
        
        .card {
            background: white;
            border-radius: 16px;
            padding: 1.75rem;
            border: 2px solid #e2e8f0;
            transition: all 0.3s ease;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
        }
        
        .card:hover {
            transform: translateY(-4px);
            box-shadow: 0 12px 24px rgba(0, 0, 0, 0.12);
            border-color: #667eea;
        }
        
        .card.error {
            background: #fff5f5;
            border-color: #fc8181;
            color: #c53030;
            padding: 2rem;
            text-align: center;
            font-weight: 500;
        }
        
        .card-head {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid #f7fafc;
        }
        
        .title {
            font-size: 1.25rem;
            font-weight: 700;
            color: #2d3748;
        }
        
        .badge {
            padding: 0.375rem 0.875rem;
            border-radius: 8px;
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .badge.ok {
            background: #c6f6d5;
            color: #22543d;
        }
        
        .badge.warn {
            background: #feebc8;
            color: #7c2d12;
        }
        
        .badge.err {
            background: #fed7d7;
            color: #742a2a;
        }
        
        .meta {
            display: grid;
            gap: 0.875rem;
            margin-bottom: 1.5rem;
        }
        
        .meta > div {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.75rem;
            background: #f7fafc;
            border-radius: 8px;
        }
        
        .meta span {
            color: #718096;
            font-size: 0.875rem;
            font-weight: 500;
        }
        
        .meta strong {
            color: #2d3748;
            font-weight: 600;
        }
        
        .meta strong.assigned {
            color: #667eea;
            font-weight: 700;
        }
        
        .actions {
            display: flex;
            gap: 0.75rem;
            flex-wrap: wrap;
            margin-bottom: 0.75rem;
        }
        
        .edit-actions {
            display: flex;
            gap: 0.75rem;
        }
        
        button {
            flex: 1;
            min-width: 100px;
            padding: 0.75rem 1.25rem;
            border: none;
            border-radius: 10px;
            font-size: 0.925rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
        }
        
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 16px rgba(102, 126, 234, 0.4);
        }
        
        button:active {
            transform: translateY(0);
        }
        
        button.ghost {
            background: white;
            color: #667eea;
            border: 2px solid #e2e8f0;
            box-shadow: none;
        }
        
        button.ghost:hover {
            border-color: #667eea;
            background: #f7fafc;
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.15);
        }
        
        button.edit {
            background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);
            box-shadow: 0 4px 12px rgba(72, 187, 120, 0.3);
        }
        
        button.edit:hover {
            box-shadow: 0 6px 16px rgba(72, 187, 120, 0.4);
        }
        
        .modal {
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.6);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            backdrop-filter: blur(4px);
            animation: fadeIn 0.2s ease;
        }
        
        .modal.hidden {
            display: none;
        }
        
        @keyframes fadeIn {
            from {
                opacity: 0;
            }
            to {
                opacity: 1;
            }
        }
        
        .modal-content {
            background: white;
            padding: 2.5rem;
            border-radius: 20px;
            width: 90%;
            max-width: 500px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            animation: slideUp 0.3s ease;
            border: 2px solid #e2e8f0;
        }
        
        @keyframes slideUp {
            from {
                transform: translateY(20px);
                opacity: 0;
            }
            to {
                transform: translateY(0);
                opacity: 1;
            }
        }
        
        .modal-content h3 {
            font-size: 1.5rem;
            margin-bottom: 1.5rem;
            color: #2d3748;
            font-weight: 700;
        }
        
        .modal-content label {
            display: block;
            margin-bottom: 0.5rem;
            color: #4a5568;
            font-weight: 600;
            font-size: 0.925rem;
        }
        
        .modal-content input[type="password"],
        .modal-content input[type="text"] {
            width: 100%;
            padding: 0.875rem 1rem;
            border: 2px solid #e2e8f0;
            border-radius: 10px;
            font-size: 1rem;
            margin-bottom: 1.5rem;
            transition: all 0.3s ease;
        }
        
        .modal-content input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 4px rgba(102, 126, 234, 0.1);
        }
        
        .modal-actions {
            display: flex;
            gap: 1rem;
            margin-bottom: 1rem;
        }
        
        .result {
            margin-top: 1rem;
            padding: 1rem;
            border-radius: 10px;
            font-weight: 500;
            text-align: center;
            min-height: 20px;
        }
        
        .oktxt {
            color: #22543d;
            background: #c6f6d5;
            padding: 0.75rem 1rem;
            border-radius: 8px;
            display: block;
        }
        
        .errtxt {
            color: #742a2a;
            background: #fed7d7;
            padding: 0.75rem 1rem;
            border-radius: 8px;
            display: block;
        }
        
        @media (max-width: 768px) {
            body {
                padding: 1rem;
            }
            
            .container {
                padding: 1.5rem;
                border-radius: 16px;
            }
            
            .header h1 {
                font-size: 2rem;
            }
            
            .grid {
                grid-template-columns: 1fr;
            }
            
            .toolbar {
                flex-direction: column;
                align-items: stretch;
            }
            
            #search {
                min-width: 100%;
            }
        }
    """
    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{title}</title><style>{css}</style></head><body><div class="container">{body}</div></body></html>"""
    return HTMLResponse(content=html)


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
