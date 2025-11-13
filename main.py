# main.py
# FastAPI Droplet Manager Prototype
# - /            health
# - /register    POST: logs a client-provided event to Airtable
# - /list        GET: lists DO droplets via REST API, logs each to Airtable
# - /power/{id}  POST: control (power_on | power_off | reboot), requires ADMIN_TOKEN via Authorization: Bearer or x_admin_token
# - /dashboard   GET: HTML dashboard for managing droplets
# - /dashboard/edit POST: Edit droplet name and assignment
# - /ws          WebSocket: real-time communication for voice/vision interface
# - /voice/transcript POST: Receive transcript from Droplet 6
# - /voice/response POST: Send AI response to dashboard
# - /verifier/status GET: Get verifier status for all droplets
# - /verifier/status/{droplet_id} GET: Get verifier status for a specific droplet
# - /verifier/update POST: Receive verifier status update from Droplet 8 and broadcast to dashboard
#
# Env vars:
#   DO_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_API_KEY, AIRTABLE_TABLE, ADMIN_TOKEN, VERIFIER_BASE_URL

import os
import time
import json
from typing import Optional, Dict, Any, List
from collections import defaultdict

import requests
from fastapi import FastAPI, Request, HTTPException, Header, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

DO_TOKEN = os.getenv("DO_TOKEN")
AT_BASE = os.getenv("AIRTABLE_BASE_ID")
AT_KEY = os.getenv("AIRTABLE_API_KEY")
AT_TABLE = os.getenv("AIRTABLE_TABLE", "events")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
VERIFIER_BASE_URL = os.getenv("VERIFIER_BASE_URL", "https://drop8.fullpotential.ai")

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.sessions: Dict[str, List[WebSocket]] = defaultdict(list)
    
    async def connect(self, websocket: WebSocket, session_id: Optional[str] = None):
        await websocket.accept()
        self.active_connections.append(websocket)
        if session_id:
            self.sessions[session_id].append(websocket)
    
    def disconnect(self, websocket: WebSocket, session_id: Optional[str] = None):
        self.active_connections.remove(websocket)
        if session_id and websocket in self.sessions[session_id]:
            self.sessions[session_id].remove(websocket)
    
    async def broadcast(self, message: Dict[str, Any], session_id: Optional[str] = None):
        # Always broadcast to all active connections (dashboard)
        # Also broadcast to specific session if provided
        targets = set(self.active_connections)
        if session_id and session_id in self.sessions:
            targets.update(self.sessions[session_id])
        
        for connection in targets:
            try:
                await connection.send_json(message)
            except Exception as e:
                # Remove dead connections
                if connection in self.active_connections:
                    self.active_connections.remove(connection)
                pass

manager = ConnectionManager()

# In-memory store for verifier status updates
# Key: droplet_id, Value: verifier status dict
verifier_status_cache: Dict[int, Dict[str, Any]] = {}


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


def verifier_api(method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Call verifier API endpoint"""
    try:
        url = f"{VERIFIER_BASE_URL}{path}"
        headers = {"Content-Type": "application/json"}
        r = requests.request(method, url, headers=headers, json=json_body, timeout=10)
        r.raise_for_status()
        return r.json() if r.text else {}
    except Exception as e:
        # Silently fail - verifier might be unavailable
        return None


def get_verifier_status(droplet_id: int) -> Optional[Dict[str, Any]]:
    """Get verifier status for a droplet. Returns heartbeat/status data if available."""
    # First check cached status (from /verifier/update endpoint)
    if droplet_id in verifier_status_cache:
        cached = verifier_status_cache[droplet_id]
        # Return cached status with current timestamp if needed
        return {
            "status": cached.get("status", "unknown"),
            "test_status": cached.get("test_status", "Unknown"),
            "score": cached.get("score"),
            "last_check": cached.get("last_check", time.strftime("%Y-%m-%d %H:%M:%S")),
            "online": cached.get("online", False)
        }
    
    # If no cached status, try to get from verifier API
    try:
        result = verifier_api("GET", f"/verifier/result/{droplet_id}")
        if result and result.get("status") != "error":
            status_data = {
                "status": result.get("status", "unknown"),
                "test_status": result.get("test_status", "unknown"),
                "score": result.get("score"),
                "last_check": result.get("timestamp") or time.strftime("%Y-%m-%d %H:%M:%S"),
                "online": result.get("test_status", "").lower() in ["passed", "success", "online", "active"]
            }
            # Cache the result
            verifier_status_cache[droplet_id] = status_data
            return status_data
    except Exception:
        pass
    
    # If direct lookup fails, check if verifier is available
    # Note: The verifier needs to send status updates via POST /verifier/update
    # For now, we'll show "Unknown" status until verifier sends updates
    health = verifier_api("GET", "/verifier/health")
    if health:
        return {
            "status": "unknown",
            "test_status": "Unknown",
            "online": False,  # Unknown status until verifier sends update
            "last_check": time.strftime("%Y-%m-%d %H:%M:%S")
        }
    
    return None


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


# ---------- Voice/Vision Integration (Droplet 6) ----------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time voice/vision communication"""
    from fastapi import Query
    session_id = websocket.query_params.get("session_id")
    await manager.connect(websocket, session_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                # Echo or process message
                await manager.broadcast({"type": "message", "data": message}, session_id)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        manager.disconnect(websocket, session_id)


@app.post("/voice/transcript")
async def voice_transcript(req: Request) -> JSONResponse:
    """Receive transcript from Droplet 6 voice interface"""
    try:
        body = await req.json()
        transcript = body.get("transcript", "")
        session_id = body.get("session_id")
        timestamp = body.get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S"))
        
        if not transcript:
            raise HTTPException(status_code=400, detail="transcript field required")
        
        # Broadcast to all connected WebSocket clients
        await manager.broadcast({
            "type": "transcript",
            "transcript": transcript,
            "session_id": session_id,
            "timestamp": timestamp
        }, session_id)
        
        # Log to Airtable
        log_event(status=f"voice_transcript: {transcript[:50]}", created=timestamp)
        
        return JSONResponse({"ok": True, "received": transcript})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/voice/response")
async def voice_response(req: Request) -> JSONResponse:
    """Send AI response back to dashboard (from Droplet 6)"""
    try:
        body = await req.json()
        response = body.get("response", "")
        session_id = body.get("session_id")
        timestamp = body.get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S"))
        
        if not response:
            raise HTTPException(status_code=400, detail="response field required")
        
        # Broadcast to all connected WebSocket clients
        await manager.broadcast({
            "type": "ai_response",
            "response": response,
            "session_id": session_id,
            "timestamp": timestamp
        }, session_id)
        
        # Log to Airtable
        log_event(status=f"ai_response: {response[:50]}", created=timestamp)
        
        return JSONResponse({"ok": True, "sent": response})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- Verifier Integration (Droplet 8) ----------
@app.get("/verifier/status")
def verifier_status() -> JSONResponse:
    """Get verifier status for all droplets"""
    try:
        data = do_api("GET", "/droplets")
        droplets = data.get("droplets", [])
        results: List[Dict[str, Any]] = []
        
        for d in droplets:
            droplet_id = d.get("id")
            verifier_data = get_verifier_status(droplet_id)
            
            result = {
                "droplet_id": droplet_id,
                "name": d.get("name"),
                "verifier_status": verifier_data
            }
            results.append(result)
        
        return JSONResponse({"count": len(results), "droplets": results})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/verifier/status/{droplet_id}")
def verifier_status_by_id(droplet_id: int) -> JSONResponse:
    """Get verifier status for a specific droplet"""
    verifier_data = get_verifier_status(droplet_id)
    return JSONResponse({
        "droplet_id": droplet_id,
        "verifier_status": verifier_data
    })


@app.post("/verifier/update")
async def verifier_update(req: Request) -> JSONResponse:
    """Receive verifier status update from Droplet 8 and broadcast to dashboard"""
    try:
        body = await req.json()
        droplet_id = body.get("droplet_id")
        verifier_status = body.get("verifier_status", {})
        timestamp = body.get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S"))
        
        if not droplet_id:
            raise HTTPException(status_code=400, detail="droplet_id field required")
        
        # Store verifier status in cache
        verifier_status_cache[droplet_id] = {
            "status": verifier_status.get("status", "unknown"),
            "test_status": verifier_status.get("test_status", "Unknown"),
            "score": verifier_status.get("score"),
            "last_check": timestamp,
            "online": verifier_status.get("online", False)
        }
        
        # Broadcast to all connected WebSocket clients
        await manager.broadcast({
            "type": "verifier_status",
            "droplet_id": droplet_id,
            "verifier_status": verifier_status,
            "timestamp": timestamp
        })
        
        # Log to Airtable
        log_event(droplet_id=droplet_id, status=f"verifier_update: {verifier_status.get('test_status', 'unknown')}", created=timestamp)
        
        return JSONResponse({"ok": True, "received": {"droplet_id": droplet_id, "verifier_status": verifier_status}})
    except Exception as e:
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
        
        # Get verifier status for this droplet
        verifier_data = get_verifier_status(droplet_id)
        verifier_online = verifier_data.get("online", False) if verifier_data else False
        verifier_last_check = verifier_data.get("last_check", "Never") if verifier_data else "Never"
        verifier_test_status = verifier_data.get("test_status", "Unknown") if verifier_data else "Unknown"
        # Show "Unknown" if verifier is available but no specific status, "Offline" if verifier unavailable
        verifier_status_cls = "ok" if verifier_online else "warn" if verifier_data and verifier_data.get("status") == "unknown" else "err"
        verifier_status_text = "🟢 Online" if verifier_online else "⚪ Unknown" if verifier_data and verifier_data.get("status") == "unknown" else "🔴 Offline"
        
        cards.append(f"""
        <div class="card" data-droplet-id="{droplet_id}">
          <div class="card-head">
            <div class="title">{name}</div>
            <span class="badge {badge_cls}">{status}</span>
          </div>
          <div class="meta">
            <div><span>ID</span><strong>{droplet_id}</strong></div>
            <div><span>IP</span><strong>{ip}</strong></div>
            <div><span>Assigned To</span><strong class="assigned">{assigned_to}</strong></div>
            <div><span>Created</span><strong>{created}</strong></div>
            <div class="verifier-status">
              <span>Verifier Status</span>
              <strong class="verifier-indicator {verifier_status_cls}">{verifier_status_text}</strong>
            </div>
            <div class="verifier-meta">
              <span>Last Check</span>
              <strong class="verifier-timestamp">{verifier_last_check}</strong>
            </div>
            <div class="verifier-meta">
              <span>Test Status</span>
              <strong class="verifier-test">{verifier_test_status}</strong>
            </div>
          </div>
          <div class="actions">
            <button onclick="openModal({droplet_id},'reboot')">Reboot</button>
            <button class="ghost" onclick="openModal({droplet_id},'power_off')">Power Off</button>
            <button class="ghost" onclick="openModal({droplet_id},'power_on')">Power On</button>
          </div>
          <div class="edit-actions">
            <button class="edit" onclick="openEditModal({droplet_id},'{name}','{assigned_to}')">✏️ Edit Name/Assignment</button>
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

    <!-- Voice/Vision Interface Panel -->
    <div class="voice-panel">
      <div class="voice-header">
        <h2>🎤 Voice & Vision Interface</h2>
        <div class="connection-status">
          <span id="wsStatus" class="status-indicator disconnected">Disconnected</span>
        </div>
      </div>
      <div class="voice-content">
        <div class="transcript-section">
          <h3>Live Transcript</h3>
          <div id="transcriptDisplay" class="transcript-display">
            <p class="placeholder">Waiting for voice input...</p>
          </div>
        </div>
        <div class="response-section">
          <h3>AI Response</h3>
          <div id="aiResponseDisplay" class="response-display">
            <p class="placeholder">Waiting for AI response...</p>
          </div>
        </div>
      </div>
    </div>

    <!-- Power Action Modal -->
    <div id="powerModal" class="modal hidden">
      <div class="modal-content">
        <h3 id="powerModalTitle">Power Action</h3>
        <label for="adminToken">Admin Token:</label>
        <input type="password" id="adminToken" placeholder="Enter admin token"/>
        <div class="modal-actions">
          <button onclick="confirmPowerAction()">Confirm</button>
          <button class="ghost" onclick="closeModal('powerModal')">Cancel</button>
        </div>
        <div id="powerResult" class="result"></div>
      </div>
    </div>

    <!-- Edit Modal -->
    <div id="editModal" class="modal hidden">
      <div class="modal-content">
        <h3>Edit Droplet</h3>
        <label for="editName">Name:</label>
        <input type="text" id="editName" placeholder="Droplet name"/>
        <label for="editAssigned">Assigned To:</label>
        <input type="text" id="editAssigned" placeholder="Assigned to (optional)"/>
        <label for="editAdminToken">Admin Token:</label>
        <input type="password" id="editAdminToken" placeholder="Enter admin token"/>
        <div class="modal-actions">
          <button onclick="confirmEdit()">Save</button>
          <button class="ghost" onclick="closeModal('editModal')">Cancel</button>
        </div>
        <div id="editResult" class="result"></div>
      </div>
    </div>

    <script>
      let currentDropletId = null;
      let currentAction = null;

      function openModal(dropletId, action) {{
        currentDropletId = dropletId;
        currentAction = action;
        const actionNames = {{
          'reboot': 'Reboot',
          'power_off': 'Power Off',
          'power_on': 'Power On'
        }};
        document.getElementById('powerModalTitle').textContent = actionNames[action] + ' Droplet';
        document.getElementById('powerModal').classList.remove('hidden');
        document.getElementById('adminToken').value = '';
        document.getElementById('powerResult').innerHTML = '';
      }}

      function openEditModal(dropletId, name, assigned) {{
        currentDropletId = dropletId;
        document.getElementById('editName').value = name;
        document.getElementById('editAssigned').value = assigned === 'Unassigned' ? '' : assigned;
        document.getElementById('editModal').classList.remove('hidden');
        document.getElementById('editAdminToken').value = '';
        document.getElementById('editResult').innerHTML = '';
      }}

      function closeModal(modalId) {{
        document.getElementById(modalId).classList.add('hidden');
        currentDropletId = null;
        currentAction = null;
      }}

      async function confirmPowerAction() {{
        const token = document.getElementById('adminToken').value;
        if (!token) {{
          document.getElementById('powerResult').innerHTML = '<span class="errtxt">Please enter admin token</span>';
          return;
        }}
        
        const resultDiv = document.getElementById('powerResult');
        resultDiv.innerHTML = '<span>Processing...</span>';
        
        try {{
          const response = await fetch(`/power/${{currentDropletId}}?action=${{currentAction}}`, {{
            method: 'POST',
            headers: {{
              'Authorization': `Bearer ${{token}}`,
              'Content-Type': 'application/json'
            }}
          }});
          
          const data = await response.json();
          if (response.ok) {{
            resultDiv.innerHTML = '<span class="oktxt">Action successful!</span>';
            setTimeout(() => {{
              closeModal('powerModal');
              location.reload();
            }}, 1500);
          }} else {{
            resultDiv.innerHTML = `<span class="errtxt">Error: ${{data.detail || 'Unknown error'}}</span>`;
          }}
        }} catch (error) {{
          resultDiv.innerHTML = `<span class="errtxt">Error: ${{error.message}}</span>`;
        }}
      }}

      async function confirmEdit() {{
        const name = document.getElementById('editName').value.trim();
        const assigned = document.getElementById('editAssigned').value.trim();
        const token = document.getElementById('editAdminToken').value;
        
        if (!name) {{
          document.getElementById('editResult').innerHTML = '<span class="errtxt">Name is required</span>';
          return;
        }}
        
        if (!token) {{
          document.getElementById('editResult').innerHTML = '<span class="errtxt">Please enter admin token</span>';
          return;
        }}
        
        const resultDiv = document.getElementById('editResult');
        resultDiv.innerHTML = '<span>Processing...</span>';
        
        try {{
          const formData = new FormData();
          formData.append('droplet_id', currentDropletId);
          formData.append('name', name);
          formData.append('assigned_to', assigned);
          formData.append('admin_token', token);
          
          const response = await fetch('/dashboard/edit', {{
            method: 'POST',
            body: formData
          }});
          
          const html = await response.text();
          resultDiv.innerHTML = html;
          
          if (response.ok && html.includes('oktxt')) {{
            setTimeout(() => {{
              closeModal('editModal');
              location.reload();
            }}, 1500);
          }}
        }} catch (error) {{
          resultDiv.innerHTML = `<span class="errtxt">Error: ${{error.message}}</span>`;
        }}
      }}

      function filterCards() {{
        const search = document.getElementById('search').value.toLowerCase();
        const cards = document.querySelectorAll('.card');
        cards.forEach(card => {{
          const text = card.textContent.toLowerCase();
          if (text.includes(search)) {{
            card.style.display = '';
          }} else {{
            card.style.display = 'none';
          }}
        }});
      }}

      // WebSocket connection for voice/vision interface
      let ws = null;
      let wsReconnectAttempts = 0;
      const maxReconnectAttempts = 5;
      let useFallback = false;

      function connectWebSocket() {{
        let wsUrl;
        const isHttps = window.location.protocol === 'https:';
        
        // For HTTPS pages, MUST use WSS on domain (browser security requirement)
        // NEVER use direct IP for HTTPS pages - it won't have SSL
        if (isHttps) {{
          // HTTPS pages: always use WSS on domain only
          wsUrl = 'wss://' + window.location.host + '/ws';
        }} else {{
          // HTTP pages: try domain first, fallback to direct IP if needed
          if (useFallback) {{
            // Use direct IP for WebSocket (bypass proxy issues)
            wsUrl = 'ws://146.190.151.40/ws';
          }} else {{
            // Try domain first
            wsUrl = 'ws://' + window.location.host + '/ws';
          }}
        }}
        
        try {{
          console.log('Attempting WebSocket connection to:', wsUrl);
          ws = new WebSocket(wsUrl);
          
          ws.onopen = () => {{
            console.log('WebSocket connected successfully to:', wsUrl);
            document.getElementById('wsStatus').textContent = 'Connected';
            document.getElementById('wsStatus').className = 'status-indicator connected';
            wsReconnectAttempts = 0;
          }};
          
          ws.onmessage = (event) => {{
            try {{
              const data = JSON.parse(event.data);
              handleWebSocketMessage(data);
            }} catch (e) {{
              console.error('Error parsing WebSocket message:', e);
            }}
          }};
          
          ws.onerror = (error) => {{
            console.error('WebSocket error:', error, 'URL:', wsUrl);
            document.getElementById('wsStatus').textContent = 'Error';
            document.getElementById('wsStatus').className = 'status-indicator error';
            
            // Only try fallback for HTTP pages (HTTPS pages can't use direct IP)
            if (!isHttps && !useFallback) {{
              console.log('Domain WebSocket failed, trying fallback...');
              useFallback = true;
              setTimeout(connectWebSocket, 1000);
            }}
          }};
          
          ws.onclose = (event) => {{
            console.log('WebSocket disconnected. Code:', event.code, 'Reason:', event.reason);
            document.getElementById('wsStatus').textContent = 'Disconnected';
            document.getElementById('wsStatus').className = 'status-indicator disconnected';
            
            // Only try fallback for HTTP pages (HTTPS pages can't use direct IP)
            if (!isHttps && !useFallback && event.code !== 1000) {{
              console.log('Trying fallback WebSocket connection...');
              useFallback = true;
              setTimeout(connectWebSocket, 1000);
              return;
            }}
            
            // Attempt to reconnect (for HTTPS, always retry domain WSS)
            // Note: If WebSocket fails, it's likely a reverse proxy (nginx/Cloudflare) configuration issue
            if (wsReconnectAttempts < maxReconnectAttempts) {{
              wsReconnectAttempts++;
              console.log('Reconnecting WebSocket (attempt ' + wsReconnectAttempts + ')...');
              setTimeout(connectWebSocket, 2000 * wsReconnectAttempts);
            }} else {{
              console.error('WebSocket connection failed after ' + maxReconnectAttempts + ' attempts. This is likely a reverse proxy configuration issue.');
              document.getElementById('wsStatus').textContent = 'Connection Failed';
              document.getElementById('wsStatus').className = 'status-indicator error';
            }}
          }};
        }} catch (error) {{
          console.error('Failed to create WebSocket:', error);
          // Only try fallback for HTTP pages (HTTPS pages can't use direct IP)
          if (!isHttps && !useFallback) {{
            useFallback = true;
            setTimeout(connectWebSocket, 1000);
          }}
        }}
      }}

      function handleWebSocketMessage(data) {{
        if (data.type === 'transcript') {{
          const transcriptDiv = document.getElementById('transcriptDisplay');
          const timestamp = new Date(data.timestamp || Date.now()).toLocaleTimeString();
          const transcript = data.transcript || '';
          transcriptDiv.innerHTML = '<div class="transcript-item"><span class="timestamp">' + timestamp + '</span><p class="transcript-text">' + transcript + '</p></div>';
        }} else if (data.type === 'ai_response') {{
          const responseDiv = document.getElementById('aiResponseDisplay');
          const timestamp = new Date(data.timestamp || Date.now()).toLocaleTimeString();
          const response = data.response || '';
          responseDiv.innerHTML = '<div class="response-item"><span class="timestamp">' + timestamp + '</span><p class="response-text">' + response + '</p></div>';
        }} else if (data.type === 'verifier_status') {{
          // Update verifier status for a specific droplet via WebSocket
          const dropletId = data.droplet_id;
          const verifierStatus = data.verifier_status;
          if (dropletId && verifierStatus) {{
            const card = document.querySelector(`[data-droplet-id="${{dropletId}}"]`);
            if (card) {{
              const online = verifierStatus.online || false;
              const lastCheck = verifierStatus.last_check || 'Never';
              const testStatus = verifierStatus.test_status || 'Unknown';
              
              // Update verifier status indicator
              const indicator = card.querySelector('.verifier-indicator');
              if (indicator) {{
                const statusCls = online ? 'ok' : (verifierStatus ? 'warn' : 'err');
                const statusText = online ? '🟢 Online' : (verifierStatus ? '🔴 Offline' : '⚪ Unknown');
                indicator.className = 'verifier-indicator ' + statusCls;
                indicator.textContent = statusText;
              }}
              
              // Update last check timestamp
              const timestamp = card.querySelector('.verifier-timestamp');
              if (timestamp) {{
                timestamp.textContent = lastCheck;
              }}
              
              // Update test status
              const testStatusEl = card.querySelector('.verifier-test');
              if (testStatusEl) {{
                testStatusEl.textContent = testStatus;
              }}
            }}
          }}
        }}
      }}

      // Verifier status update function
      async function updateVerifierStatus() {{
        try {{
          const response = await fetch('/verifier/status');
          const data = await response.json();
          
          if (data.droplets) {{
            data.droplets.forEach(droplet => {{
              const card = document.querySelector(`[data-droplet-id="${{droplet.droplet_id}}"]`);
              if (card && droplet.verifier_status) {{
                const verifierStatus = droplet.verifier_status;
                const online = verifierStatus.online || false;
                const lastCheck = verifierStatus.last_check || 'Never';
                const testStatus = verifierStatus.test_status || 'Unknown';
                
                // Update verifier status indicator
                const indicator = card.querySelector('.verifier-indicator');
                if (indicator) {{
                  const statusCls = online ? 'ok' : (verifierStatus ? 'warn' : 'err');
                  const statusText = online ? '🟢 Online' : (verifierStatus ? '🔴 Offline' : '⚪ Unknown');
                  indicator.className = 'verifier-indicator ' + statusCls;
                  indicator.textContent = statusText;
                }}
                
                // Update last check timestamp
                const timestamp = card.querySelector('.verifier-timestamp');
                if (timestamp) {{
                  timestamp.textContent = lastCheck;
                }}
                
                // Update test status
                const testStatusEl = card.querySelector('.verifier-test');
                if (testStatusEl) {{
                  testStatusEl.textContent = testStatus;
                }}
              }}
            }});
          }}
        }} catch (error) {{
          console.error('Failed to update verifier status:', error);
        }}
      }}
      
      // Connect WebSocket when page loads
      window.addEventListener('load', () => {{
        connectWebSocket();
        // Update verifier status on load
        updateVerifierStatus();
        // Update verifier status every 30 seconds
        setInterval(updateVerifierStatus, 30000);
      }});
    </script>
    """

    return html_page("Droplet Dashboard", body)


@app.post("/dashboard/edit", response_class=HTMLResponse)
def dashboard_edit(
    droplet_id: int = Form(...),
    name: str = Form(...),
    assigned_to: str = Form(default=""),
    admin_token: str = Form(...),
) -> HTMLResponse:
    if not ADMIN_TOKEN:
        return HTMLResponse("<span class='errtxt'>Server configuration error</span>", status_code=500)
    
    # Check if token is provided
    if not admin_token:
        return HTMLResponse("<span class='errtxt'>Unauthorized: Admin token required</span>", status_code=401)
    
    # Normalize tokens for comparison (strip whitespace)
    provided_token = admin_token.strip()
    expected_token = ADMIN_TOKEN.strip()
    
    # Compare tokens
    if provided_token != expected_token:
        # For debugging (remove in production)
        import hashlib
        provided_hash = hashlib.md5(provided_token.encode()).hexdigest()[:8]
        expected_hash = hashlib.md5(expected_token.encode()).hexdigest()[:8]
        return HTMLResponse(f"<span class='errtxt'>Unauthorized: Token mismatch (provided: ...{provided_hash}, expected: ...{expected_hash})</span>", status_code=401)

    try:
        # Get current droplet data
        data = do_api("GET", f"/droplets/{droplet_id}")
        droplet = data.get("droplet", {})
        current_tags = droplet.get("tags", [])
        
        # Remove any existing assigned: tags
        tags = [tag for tag in current_tags if not tag.startswith("assigned:")]
        
        # Add new assigned tag if provided
        if assigned_to.strip():
            new_tag = f"assigned:{assigned_to.strip()}"
            tags.append(new_tag)
            
            # Create the tag if it doesn't exist
            try:
                do_api("POST", "/tags", json_body={"name": new_tag})
            except:
                pass  # Tag might already exist
        
        # Update tags - assign all tags to the droplet
        # DO API format: POST /tags/{tag_name}/resources
        # First, remove old assigned: tags
        for old_tag in current_tags:
            if old_tag.startswith("assigned:"):
                try:
                    do_api("DELETE", f"/tags/{old_tag}/resources", json_body={"resources": [{"resource_id": str(droplet_id), "resource_type": "droplet"}]})
                except:
                    pass  # Tag might not exist or already removed
        
        # Then, assign new assigned: tag if provided
        if assigned_to.strip():
            new_tag = f"assigned:{assigned_to.strip()}"
            try:
                do_api("POST", f"/tags/{new_tag}/resources", json_body={"resources": [{"resource_id": str(droplet_id), "resource_type": "droplet"}]})
            except:
                pass  # Tag assignment might fail if tag doesn't exist
        
        # Store name in Airtable (DO API doesn't support name updates directly)
        log_event(droplet_id=droplet_id, name=name, assigned_to=assigned_to.strip(), status="updated")
        return HTMLResponse("<span class='oktxt'>Updated successfully! (Note: Name stored in Airtable, tags updated)</span>")
    except requests.HTTPError as e:
        error_msg = f"DO error: {e.response.status_code}"
        try:
            error_detail = e.response.json().get("message", e.response.text)
            error_msg = f"{error_msg} - {error_detail}"
        except:
            error_msg = f"{error_msg} - {e.response.text}"
        return HTMLResponse(f"<span class='errtxt'>{error_msg}</span>", status_code=500)
    except Exception as e:
        return HTMLResponse(f"<span class='errtxt'>Error: {str(e)}</span>", status_code=500)


def html_page(title: str, body: str) -> HTMLResponse:
    """Premium, elegant UI with modern design"""
    css = """
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        :root {
            --primary-gradient: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --secondary-gradient: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            --success-gradient: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
            --card-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
            --card-shadow-hover: 0 12px 40px rgba(102, 126, 234, 0.15);
            --border-radius: 20px;
            --transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            background-attachment: fixed;
            min-height: 100vh;
            padding: 2rem;
            color: #1a202c;
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
        }
        
        .container {
            max-width: 1600px;
            margin: 0 auto;
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(20px);
            border-radius: var(--border-radius);
            padding: 3.5rem;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.8);
            animation: fadeInUp 0.6s ease-out;
        }
        
        @keyframes fadeInUp {
            from {
                opacity: 0;
                transform: translateY(30px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        
        .header {
            margin-bottom: 3.5rem;
            padding-bottom: 2.5rem;
            border-bottom: 2px solid rgba(226, 232, 240, 0.6);
            position: relative;
        }
        
        .header::after {
            content: '';
            position: absolute;
            bottom: -2px;
            left: 0;
            width: 100px;
            height: 3px;
            background: var(--primary-gradient);
            border-radius: 2px;
        }
        
        .header h1 {
            font-size: 3rem;
            font-weight: 800;
            background: var(--primary-gradient);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 0.75rem;
            letter-spacing: -0.02em;
            line-height: 1.2;
        }
        
        .sub {
            color: #64748b;
            font-size: 1.15rem;
            margin-bottom: 2rem;
            font-weight: 400;
            line-height: 1.6;
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
            padding: 1rem 1.5rem;
            border: 2px solid rgba(226, 232, 240, 0.8);
            border-radius: 16px;
            font-size: 1rem;
            transition: var(--transition);
            background: rgba(255, 255, 255, 0.9);
            backdrop-filter: blur(10px);
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
        }
        
        #search:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 4px rgba(102, 126, 234, 0.15), 0 4px 16px rgba(102, 126, 234, 0.1);
            background: white;
            transform: translateY(-1px);
        }
        
        #search::placeholder {
            color: #94a3b8;
        }
        
        .count {
            padding: 1rem 2rem;
            background: var(--primary-gradient);
            color: white;
            border-radius: 16px;
            font-weight: 700;
            font-size: 1rem;
            box-shadow: 0 8px 24px rgba(102, 126, 234, 0.35);
            letter-spacing: 0.3px;
            transition: var(--transition);
        }
        
        .count:hover {
            transform: translateY(-2px);
            box-shadow: 0 12px 32px rgba(102, 126, 234, 0.4);
        }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
            gap: 2rem;
        }
        
        .card {
            background: linear-gradient(135deg, rgba(255, 255, 255, 0.95) 0%, rgba(255, 255, 255, 0.9) 100%);
            backdrop-filter: blur(20px);
            border-radius: var(--border-radius);
            padding: 2rem;
            border: 1px solid rgba(226, 232, 240, 0.6);
            transition: var(--transition);
            box-shadow: var(--card-shadow);
            position: relative;
            overflow: hidden;
            animation: fadeInUp 0.5s ease-out backwards;
        }
        
        .card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            background: var(--primary-gradient);
            opacity: 0;
            transition: var(--transition);
        }
        
        .card:hover {
            transform: translateY(-8px) scale(1.02);
            box-shadow: var(--card-shadow-hover);
            border-color: rgba(102, 126, 234, 0.3);
        }
        
        .card:hover::before {
            opacity: 1;
        }
        
        @keyframes fadeInUp {
            from {
                opacity: 0;
                transform: translateY(20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
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
            margin-bottom: 1.75rem;
            padding-bottom: 1.25rem;
            border-bottom: 2px solid rgba(247, 250, 252, 0.8);
        }
        
        .title {
            font-size: 1.4rem;
            font-weight: 700;
            color: #1e293b;
            letter-spacing: -0.01em;
        }
        
        .badge {
            padding: 0.5rem 1rem;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
            transition: var(--transition);
        }
        
        .badge.ok {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
        }
        
        .badge.warn {
            background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
            color: white;
        }
        
        .badge.err {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            color: white;
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
            padding: 1rem 1.25rem;
            background: linear-gradient(135deg, rgba(247, 250, 252, 0.8) 0%, rgba(241, 245, 249, 0.6) 100%);
            border-radius: 12px;
            border: 1px solid rgba(226, 232, 240, 0.5);
            transition: var(--transition);
        }
        
        .meta > div:hover {
            background: linear-gradient(135deg, rgba(255, 255, 255, 0.9) 0%, rgba(247, 250, 252, 0.8) 100%);
            transform: translateX(4px);
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
        }
        
        .meta span {
            color: #64748b;
            font-size: 0.875rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .meta strong {
            color: #1e293b;
            font-weight: 700;
            font-size: 0.95rem;
        }
        
        .meta strong.assigned {
            color: #667eea;
            font-weight: 800;
            font-size: 1rem;
        }
        
        .verifier-status {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1rem 1.25rem;
            background: linear-gradient(135deg, rgba(247, 250, 252, 0.9) 0%, rgba(241, 245, 249, 0.7) 100%);
            border-radius: 12px;
            border: 2px solid rgba(226, 232, 240, 0.6);
            transition: var(--transition);
            margin-top: 0.5rem;
        }
        
        .verifier-status:hover {
            background: linear-gradient(135deg, rgba(255, 255, 255, 0.95) 0%, rgba(247, 250, 252, 0.9) 100%);
            transform: translateX(4px);
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
        }
        
        .verifier-indicator {
            font-weight: 800;
            font-size: 1rem;
            padding: 0.5rem 1rem;
            border-radius: 10px;
            transition: var(--transition);
        }
        
        .verifier-indicator.ok {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
        }
        
        .verifier-indicator.warn {
            background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
            color: white;
            box-shadow: 0 4px 12px rgba(245, 158, 11, 0.3);
        }
        
        .verifier-indicator.err {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            color: white;
            box-shadow: 0 4px 12px rgba(239, 68, 68, 0.3);
        }
        
        .verifier-meta {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.75rem 1.25rem;
            background: linear-gradient(135deg, rgba(247, 250, 252, 0.6) 0%, rgba(241, 245, 249, 0.4) 100%);
            border-radius: 10px;
            border: 1px solid rgba(226, 232, 240, 0.4);
            transition: var(--transition);
            font-size: 0.85rem;
        }
        
        .verifier-meta:hover {
            background: linear-gradient(135deg, rgba(255, 255, 255, 0.8) 0%, rgba(247, 250, 252, 0.6) 100%);
            transform: translateX(2px);
        }
        
        .verifier-timestamp {
            color: #64748b;
            font-weight: 600;
            font-size: 0.9rem;
        }
        
        .verifier-test {
            color: #475569;
            font-weight: 700;
            font-size: 0.9rem;
            text-transform: capitalize;
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
            padding: 0.875rem 1.5rem;
            border: none;
            border-radius: 14px;
            font-size: 0.95rem;
            font-weight: 700;
            cursor: pointer;
            transition: var(--transition);
            background: var(--primary-gradient);
            color: white;
            box-shadow: 0 6px 20px rgba(102, 126, 234, 0.35);
            letter-spacing: 0.3px;
            position: relative;
            overflow: hidden;
        }
        
        button::before {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            width: 0;
            height: 0;
            border-radius: 50%;
            background: rgba(255, 255, 255, 0.3);
            transform: translate(-50%, -50%);
            transition: width 0.6s, height 0.6s;
        }
        
        button:hover {
            transform: translateY(-3px) scale(1.02);
            box-shadow: 0 10px 30px rgba(102, 126, 234, 0.45);
        }
        
        button:hover::before {
            width: 300px;
            height: 300px;
        }
        
        button:active {
            transform: translateY(-1px) scale(0.98);
        }
        
        button.ghost {
            background: rgba(255, 255, 255, 0.9);
            color: #667eea;
            border: 2px solid rgba(102, 126, 234, 0.3);
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
        }
        
        button.ghost:hover {
            border-color: #667eea;
            background: white;
            box-shadow: 0 6px 20px rgba(102, 126, 234, 0.25);
            transform: translateY(-3px);
        }
        
        button.edit {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            box-shadow: 0 6px 20px rgba(16, 185, 129, 0.35);
        }
        
        button.edit:hover {
            box-shadow: 0 10px 30px rgba(16, 185, 129, 0.45);
        }
        
        .modal {
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.7);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            backdrop-filter: blur(8px);
            animation: fadeIn 0.3s cubic-bezier(0.4, 0, 0.2, 1);
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
            background: linear-gradient(135deg, rgba(255, 255, 255, 0.98) 0%, rgba(255, 255, 255, 0.95) 100%);
            backdrop-filter: blur(30px);
            padding: 3rem;
            border-radius: 24px;
            width: 90%;
            max-width: 550px;
            box-shadow: 0 25px 80px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(255, 255, 255, 0.5);
            animation: slideUp 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            border: 1px solid rgba(226, 232, 240, 0.8);
            position: relative;
            overflow: hidden;
        }
        
        .modal-content::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            background: var(--primary-gradient);
        }
        
        @keyframes slideUp {
            from {
                transform: translateY(40px) scale(0.95);
                opacity: 0;
            }
            to {
                transform: translateY(0) scale(1);
                opacity: 1;
            }
        }
        
        .modal-content h3 {
            font-size: 1.75rem;
            margin-bottom: 2rem;
            color: #1e293b;
            font-weight: 800;
            letter-spacing: -0.02em;
            background: var(--primary-gradient);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .modal-content label {
            display: block;
            margin-bottom: 0.75rem;
            color: #475569;
            font-weight: 700;
            font-size: 0.95rem;
            letter-spacing: 0.2px;
        }
        
        .modal-content input[type="password"],
        .modal-content input[type="text"] {
            width: 100%;
            padding: 1rem 1.25rem;
            border: 2px solid rgba(226, 232, 240, 0.8);
            border-radius: 14px;
            font-size: 1rem;
            margin-bottom: 1.75rem;
            transition: var(--transition);
            background: rgba(255, 255, 255, 0.9);
            backdrop-filter: blur(10px);
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
        }
        
        .modal-content input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 4px rgba(102, 126, 234, 0.15), 0 4px 16px rgba(102, 126, 234, 0.1);
            background: white;
            transform: translateY(-1px);
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
            color: #065f46;
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.15) 0%, rgba(5, 150, 105, 0.1) 100%);
            padding: 1rem 1.25rem;
            border-radius: 12px;
            display: block;
            border: 1px solid rgba(16, 185, 129, 0.3);
            font-weight: 600;
            box-shadow: 0 2px 8px rgba(16, 185, 129, 0.15);
        }
        
        .errtxt {
            color: #991b1b;
            background: linear-gradient(135deg, rgba(239, 68, 68, 0.15) 0%, rgba(220, 38, 38, 0.1) 100%);
            padding: 1rem 1.25rem;
            border-radius: 12px;
            display: block;
            border: 1px solid rgba(239, 68, 68, 0.3);
            font-weight: 600;
            box-shadow: 0 2px 8px rgba(239, 68, 68, 0.15);
        }
        
        /* Voice/Vision Panel Styles */
        .voice-panel {
            margin-top: 4rem;
            padding: 3rem;
            background: linear-gradient(135deg, rgba(255, 255, 255, 0.95) 0%, rgba(255, 255, 255, 0.9) 100%);
            backdrop-filter: blur(20px);
            border-radius: var(--border-radius);
            border: 1px solid rgba(226, 232, 240, 0.6);
            box-shadow: var(--card-shadow);
            position: relative;
            overflow: hidden;
            animation: fadeInUp 0.6s ease-out 0.2s backwards;
        }
        
        .voice-panel::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            background: var(--primary-gradient);
        }
        
        .voice-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2.5rem;
            padding-bottom: 1.5rem;
            border-bottom: 2px solid rgba(226, 232, 240, 0.6);
            position: relative;
        }
        
        .voice-header::after {
            content: '';
            position: absolute;
            bottom: -2px;
            left: 0;
            width: 80px;
            height: 3px;
            background: var(--primary-gradient);
            border-radius: 2px;
        }
        
        .voice-header h2 {
            font-size: 2rem;
            font-weight: 800;
            color: #1e293b;
            margin: 0;
            letter-spacing: -0.02em;
            background: var(--primary-gradient);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .connection-status {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        
        .status-indicator {
            padding: 0.625rem 1.25rem;
            border-radius: 14px;
            font-size: 0.8rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
            transition: var(--transition);
        }
        
        .status-indicator.connected {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
            animation: pulse 2s infinite;
        }
        
        .status-indicator.disconnected {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            color: white;
        }
        
        .status-indicator.error {
            background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
            color: white;
        }
        
        @keyframes pulse {
            0%, 100% {
                opacity: 1;
            }
            50% {
                opacity: 0.8;
            }
        }
        
        .voice-content {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 2.5rem;
        }
        
        .transcript-section, .response-section {
            display: flex;
            flex-direction: column;
        }
        
        .transcript-section h3, .response-section h3 {
            font-size: 1.4rem;
            font-weight: 700;
            color: #1e293b;
            margin-bottom: 1.25rem;
            letter-spacing: -0.01em;
        }
        
        .transcript-display, .response-display {
            min-height: 250px;
            max-height: 450px;
            overflow-y: auto;
            padding: 1.5rem;
            background: linear-gradient(135deg, rgba(247, 250, 252, 0.8) 0%, rgba(241, 245, 249, 0.6) 100%);
            border-radius: 16px;
            border: 1px solid rgba(226, 232, 240, 0.6);
            box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.04);
            scrollbar-width: thin;
            scrollbar-color: rgba(102, 126, 234, 0.3) transparent;
        }
        
        .transcript-display::-webkit-scrollbar,
        .response-display::-webkit-scrollbar {
            width: 6px;
        }
        
        .transcript-display::-webkit-scrollbar-track,
        .response-display::-webkit-scrollbar-track {
            background: transparent;
        }
        
        .transcript-display::-webkit-scrollbar-thumb,
        .response-display::-webkit-scrollbar-thumb {
            background: rgba(102, 126, 234, 0.3);
            border-radius: 3px;
        }
        
        .transcript-item, .response-item {
            margin-bottom: 1.25rem;
            padding: 1.25rem;
            background: white;
            border-radius: 14px;
            border-left: 4px solid #667eea;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
            transition: var(--transition);
            animation: slideIn 0.3s ease-out;
        }
        
        .transcript-item:hover, .response-item:hover {
            transform: translateX(4px);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
        }
        
        .response-item {
            border-left-color: #10b981;
        }
        
        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateX(-10px);
            }
            to {
                opacity: 1;
                transform: translateX(0);
            }
        }
        
        .timestamp {
            display: block;
            font-size: 0.75rem;
            color: #64748b;
            margin-bottom: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .transcript-text, .response-text {
            margin: 0;
            color: #1e293b;
            line-height: 1.7;
            font-size: 0.95rem;
            font-weight: 500;
        }
        
        .placeholder {
            color: #94a3b8;
            font-style: italic;
            text-align: center;
            padding: 3rem 2rem;
            font-size: 1rem;
            font-weight: 500;
        }
        
        @media (max-width: 768px) {
            body {
                padding: 1rem;
            }
            
            .container {
                padding: 2rem;
                border-radius: 20px;
            }
            
            .header h1 {
                font-size: 2.25rem;
            }
            
            .grid {
                grid-template-columns: 1fr;
                gap: 1.5rem;
            }
            
            .toolbar {
                flex-direction: column;
                align-items: stretch;
            }
            
            #search {
                min-width: 100%;
            }
            
            .voice-content {
                grid-template-columns: 1fr;
                gap: 2rem;
            }
            
            .voice-panel {
                padding: 2rem;
            }
            
            .modal-content {
                padding: 2rem;
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

