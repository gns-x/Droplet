# main.py
# FastAPI Droplet Manager with Polished Dashboard + Control (fixed JS scoping)
# Endpoints:
# - /                 Health
# - /register         POST (logs to Airtable)
# - /list             GET (DigitalOcean API)
# - /dashboard        GET (HTML dashboard with search + action modal)
# - /dashboard/power  POST (form submit; requires ADMIN_TOKEN)
# - /power/{id}       POST (API control; Authorization: Bearer or x_admin_token)
# - /destroy/{id}     DELETE (API control; Authorization: Bearer or x_admin_token) [hidden from dashboard]
#
# Env vars: DO_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_API_KEY, AIRTABLE_TABLE, ADMIN_TOKEN

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
) -> None:
    """Log an event row to Airtable: droplet_id, name, ip, status, created."""
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
    }
    try:
        requests.post(url, headers=headers, json={"fields": fields}, timeout=10)
    except Exception:
        pass


def do_api(method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Call DigitalOcean REST API and return parsed JSON."""
    if not DO_TOKEN:
        raise HTTPException(status_code=500, detail="DO_TOKEN missing")
    url = f"https://api.digitalocean.com/v2{path}"
    headers = {"Authorization": f"Bearer {DO_TOKEN}", "Content-Type": "application/json"}
    r = requests.request(method, url, headers=headers, json=json_body, timeout=20)
    r.raise_for_status()
    return r.json() if r.text else {}


def require_admin_auth(authorization: Optional[str], x_admin_token: Optional[str]) -> None:
    """Accept Authorization: Bearer <token> or x_admin_token header."""
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
    """Accept JSON body, validate required fields, log to Airtable."""
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
    """List droplets via DigitalOcean API and log each to Airtable."""
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


# ---------- Polished Dashboard ----------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Visual dashboard with search, badges, and action modal."""
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
            <div><span>Created</span><strong>{created}</strong></div>
          </div>
          <div class="actions">
            <button onclick="openModal('{droplet_id}','reboot')">Reboot</button>
            <button class="ghost" onclick="openModal('{droplet_id}','power_off')">Power Off</button>
            <button class="ghost" onclick="openModal('{droplet_id}','power_on')">Power On</button>
          </div>
        </div>
        """)

    # Important: the JS below is static text inside the f-string; all variables are set client-side.
    body = f"""
    <div class="header">
      <h1>Droplet Dashboard</h1>
      <p class="sub">Overview of DigitalOcean droplets with status, IPs, and quick controls.</p>
      <div class="toolbar">
        <input id="search" type="text" placeholder="Search by name or IP…" oninput="filterCards()"/>
        <span class="count">Total: {len(droplets)}</span>
      </div>
    </div>
    <div id="grid" class="grid">
      {''.join(cards)}
    </div>

    <!-- Modal -->
    <div id="modal" class="modal hidden">
      <div class="modal-content">
        <h3 id="m-title">Run Action</h3>
        <form id="m-form" onsubmit="return runAction(event)">
          <input type="hidden" id="m-droplet-id" name="droplet_id" />
          <input type="hidden" id="m-action" name="action" />
          <label>ADMIN_TOKEN</label>
          <input type="password" id="m-token" name="admin_token" required />
          <div class="modal-actions">
            <button type="submit">Confirm</button>
            <button type="button" class="ghost" onclick="closeModal()">Cancel</button>
          </div>
          <div id="m-result" class="result"></div>
        </form>
      </div>
    </div>

    <script>
      function filterCards() {{
        const q = document.getElementById('search').value.toLowerCase();
        const cards = document.querySelectorAll('.card');
        cards.forEach(function(c) {{
          const text = c.innerText.toLowerCase();
          c.style.display = text.includes(q) ? '' : 'none';
        }});
      }}

      function openModal(id, actionName) {{
        document.getElementById('m-droplet-id').value = id;
        document.getElementById('m-action').value = actionName;
        document.getElementById('m-title').innerText = "Action: " + actionName + " on #" + id;
        document.getElementById('m-result').innerHTML = "";
        document.getElementById('modal').classList.remove('hidden');
      }}

      function closeModal() {{
        document.getElementById('modal').classList.add('hidden');
      }}

      async function runAction(e) {{
        e.preventDefault();
        var id = document.getElementById('m-droplet-id').value;
        var actionName = document.getElementById('m-action').value;
        var token = document.getElementById('m-token').value;

        const res = await fetch('/dashboard/power', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
          body: 'droplet_id=' + encodeURIComponent(id)
             + '&action=' + encodeURIComponent(actionName)
             + '&admin_token=' + encodeURIComponent(token)
        }});

        const html = await res.text();
        const ok = html.includes('OK:') || html.toLowerCase().includes('ok: action queued');
        document.getElementById('m-result').innerHTML = ok
          ? "<span class='oktxt'>Action sent successfully.</span>"
          : "<span class='errtxt'>Action failed. Check token or DO permissions.</span>";
        return false;
      }}
    </script>
    """
    return html_page("Droplet Dashboard", body)


@app.post("/dashboard/power", response_class=HTMLResponse)
def dashboard_power(
    droplet_id: int = Form(...),
    action: str = Form(...),
    admin_token: str = Form(...),
) -> HTMLResponse:
    """Form handler for dashboard actions."""
    if not ADMIN_TOKEN or admin_token != ADMIN_TOKEN:
        return HTMLResponse("<span class='errtxt'>Unauthorized</span>")
    if action not in {"power_on", "power_off", "reboot"}:
        return HTMLResponse("<span class='errtxt'>Invalid action</span>")
    try:
        _ = do_api("POST", f"/droplets/{droplet_id}/actions", {"type": action})
        log_event(droplet_id=droplet_id, status=f"dashboard:{action}")
        return HTMLResponse("<span class='oktxt'>OK: action queued</span>")
    except requests.HTTPError as e:
        return HTMLResponse(f"<span class='errtxt'>DO error: {e.response.status_code} {e.response.text}</span>")
    except Exception as e:
        return HTMLResponse(f"<span class='errtxt'>Error: {e}</span>")


def html_page(title: str, body: str) -> HTMLResponse:
    """Base HTML layout and styles."""
    css = """
    :root{--bg:#0a0f1f;--panel:#111938;--muted:#9aa3b2;--text:#e7eaf1;--accent:#5b8cff;--ok:#22c55e;--warn:#f59e0b;--err:#ef4444;}
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,Segoe UI,Roboto,Ubuntu,Cantarell,Arial}
    .container{max-width:1100px;margin:24px auto;padding:0 16px}
    .header h1{margin:0 0 6px} .sub{margin:0;color:var(--muted)}
    .toolbar{margin-top:12px;display:flex;gap:12px;align-items:center}
    #search{flex:1;background:#0e1530;color:var(--text);border:1px solid #1f2942;border-radius:8px;padding:10px}
    .count{color:var(--muted)}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin-top:16px}
    .card{background:var(--panel);border:1px solid #1f2942;border-radius:14px;padding:14px;box-shadow:0 6px 18px rgba(0,0,0,.2)}
    .card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
    .title{font-weight:700} .badge{padding:6px 10px;border-radius:999px;font-weight:700;font-size:12px;color:#0a0f1f}
    .badge.ok{background:var(--ok)} .badge.warn{background:var(--warn)} .badge.err{background:var(--err)}
    .meta{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:10px 0}
    .meta div{background:#0e1530;border:1px solid #1f2942;border-radius:10px;padding:10px}
    .meta span{color:var(--muted);display:block;margin-bottom:4px;font-size:12px}
    .actions{display:flex;gap:10px;margin-top:8px}
    .actions button{background:var(--accent);border:1px solid var(--accent);color:white;border-radius:8px;padding:8px 12px;cursor:pointer}
    .actions .ghost{background:#0e1530;border-color:#2b3a6a}
    .modal.hidden{display:none}
    .modal{position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px}
    .modal-content{background:var(--panel);border:1px solid #1f2942;border-radius:12px;padding:16px;max-width:420px;width:100%}
    .modal-actions{display:flex;gap:10px;margin-top:12px}
    .result{margin-top:10px}
    .oktxt{color:var(--ok)} .errtxt{color:var(--err)}
    .card.error{padding:16px}
    """
    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{title}</title><style>{css}</style></head><body><div class="container">{body}</div></body></html>"""
    return HTMLResponse(content=html)


# ---------- API Controls ----------
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

