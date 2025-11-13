# Droplet Manager Dashboard (Droplet 7)

A FastAPI-based dashboard system for managing DigitalOcean droplets with real-time updates, verifier integration, and voice/vision interface support.

## Features

- **Dashboard UI**: Modern, responsive web interface for managing droplets
- **Real-time Updates**: WebSocket support for live status updates
- **Power Control**: Start, stop, and reboot droplets
- **Verifier Integration**: Display droplet heartbeat and status from Droplet 8
- **Voice/Vision Interface**: Real-time transcript and AI response display (Droplet 6 integration)
- **Airtable Logging**: Automatic event logging to Airtable

## Prerequisites

- Docker installed on the server
- `.env` file in project root (not committed):
```env
DO_TOKEN=your_digitalocean_token
AIRTABLE_BASE_ID=your_airtable_base_id
AIRTABLE_API_KEY=your_airtable_api_key
AIRTABLE_TABLE=events
ADMIN_TOKEN=your_admin_token
VERIFIER_BASE_URL=https://drop8.fullpotential.ai
```

## Build & Run

```bash
docker build -t droplet-manager .
docker run -d -p 80:8000 --env-file .env --name droplet-manager droplet-manager
```

## API Endpoints

### Health Check
```
GET /
Response: {"status":"ok"}
```

### Register Event
```
POST /register
Header: Content-Type: application/json
Body: {"droplet_id":123,"name":"test","ip":"1.2.3.4","created":"optional-ISO8601"}
Response: {"ok": true, "received": {...}}
Airtable: adds a row with status="registered"
```

### List Droplets
```
GET /list
Response: {"count": N, "droplets": [{droplet_id, name, ip, status, created, assigned_to}]}
Airtable: logs each droplet as a row
```

### Power Control
```
POST /power/{droplet_id}?action=power_on|power_off|reboot
Header: Authorization: Bearer <ADMIN_TOKEN>
  OR
Header: x-admin-token: <ADMIN_TOKEN>
Response: {"ok": true, "action": "power_on", "droplet_id": 123, "response": {...}}
```

### Dashboard
```
GET /dashboard
Response: HTML dashboard page with:
  - Droplet cards with status, IP, assignment
  - Power control modals
  - Edit droplet name and assignment
  - Verifier status indicators (🟢 Online / 🔴 Offline)
  - Voice & Vision Interface panel
  - Real-time WebSocket connection status
```

### Edit Droplet
```
POST /dashboard/edit
Content-Type: application/x-www-form-urlencoded
Body: droplet_id=123&name=NewName&assigned_to=TeamA&admin_token=<ADMIN_TOKEN>
Response: HTML redirect or error page
```

### WebSocket
```
WS /ws
Protocol: WebSocket (ws:// or wss://)
Messages:
  - Receive: {"type": "transcript", "text": "...", "timestamp": "..."}
  - Receive: {"type": "response", "response": "...", "timestamp": "..."}
  - Receive: {"type": "verifier_status", "droplet_id": 123, "verifier_status": {...}}
```

### Voice/Vision Integration (Droplet 6)
```
POST /voice/transcript
Header: Content-Type: application/json
Body: {"text": "user transcript", "timestamp": "ISO8601", "session_id": "optional"}
Response: {"ok": true}
Broadcasts to WebSocket clients

POST /voice/response
Header: Content-Type: application/json
Body: {"response": "AI response", "timestamp": "ISO8601", "session_id": "optional"}
Response: {"ok": true}
Broadcasts to WebSocket clients
```

### Verifier Integration (Droplet 8)
```
GET /verifier/status
Response: {"count": N, "droplets": [{droplet_id, name, verifier_status: {...}}]}

GET /verifier/status/{droplet_id}
Response: {"droplet_id": 123, "verifier_status": {...}}

POST /verifier/update
Header: Content-Type: application/json
Body: {
  "droplet_id": 123,
  "verifier_status": {
    "online": true,
    "test_status": "Passed",
    "last_check": "2025-11-13T06:57:00Z"
  },
  "timestamp": "optional"
}
Response: {"ok": true, "received": {...}}
Broadcasts to WebSocket clients and caches status
```

## Features Details

### Dashboard UI
- **Modern Design**: Premium, elegant UI with glassmorphism effects
- **Real-time Updates**: WebSocket connection for live status updates
- **Power Actions**: Modal dialogs for power control (on/off/reboot)
- **Edit Functionality**: In-place editing of droplet names and assignments
- **Verifier Status**: Visual indicators showing online/offline status with last check time
- **Voice Interface**: Live transcript and AI response display panel

### Verifier Status Caching
- Status updates from Droplet 8 are cached in memory
- Dashboard polls `/verifier/status` every 30 seconds
- Real-time updates via WebSocket when `/verifier/update` is called
- Status persists across page refreshes (until server restart)

### WebSocket Connection
- Automatically connects on dashboard load
- Supports both HTTP (ws://) and HTTPS (wss://) protocols
- Auto-reconnect with exponential backoff
- Connection status indicator in dashboard header

## Deployment

### Local Development
```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Production (Docker)
```bash
# Build image
docker build -t droplet-manager .

# Run container
docker run -d -p 80:8000 --env-file .env --name droplet-manager droplet-manager

# View logs
docker logs droplet-manager --tail 100 -f

# Restart container
docker restart droplet-manager

# Rebuild and restart
docker stop droplet-manager
docker rm droplet-manager
docker build -t droplet-manager .
docker run -d -p 80:8000 --env-file .env --name droplet-manager droplet-manager
```

## Testing

### Smoke Tests
```bash
# Health check
curl https://drop7.fullpotential.ai/

# List droplets
curl https://drop7.fullpotential.ai/list

# Register event
curl -X POST https://drop7.fullpotential.ai/register \
  -H "Content-Type: application/json" \
  -d '{"droplet_id":123,"name":"test","ip":"1.2.3.4"}'

# Power control (requires ADMIN_TOKEN)
curl -X POST "https://drop7.fullpotential.ai/power/123?action=power_on" \
  -H "Authorization: Bearer <ADMIN_TOKEN>"

# Verifier status
curl https://drop7.fullpotential.ai/verifier/status

# Send verifier update
curl -X POST https://drop7.fullpotential.ai/verifier/update \
  -H "Content-Type: application/json" \
  -d '{"droplet_id":123,"verifier_status":{"online":true,"test_status":"Passed"}}'
```

## Troubleshooting

- **Check logs**: `docker logs droplet-manager --tail 100`
- **Confirm envs**: `docker exec -it droplet-manager sh -c 'env | grep -E "DO_TOKEN|AIRTABLE|ADMIN_TOKEN|VERIFIER"''`
- **Verify Airtable table**: Ensure columns exist: `droplet_id`, `name`, `ip`, `status`, `created`, `assigned_to`
- **WebSocket issues**: Check reverse proxy (nginx/Cloudflare) configuration for WebSocket upgrade support
- **Verifier status offline**: Ensure Droplet 8 is sending updates to `/verifier/update` endpoint

## Architecture

### Integration Points
- **Droplet 6 (Voice/Vision)**: Sends transcripts and receives AI responses via POST endpoints
- **Droplet 8 (Verifier)**: Sends status updates via POST `/verifier/update`, dashboard queries via GET `/verifier/status`
- **DigitalOcean API**: Fetches droplet list and controls power actions
- **Airtable**: Logs all events and operations

### WebSocket Flow
1. Dashboard connects to `/ws` on page load
2. Droplet 6 sends POST `/voice/transcript` → Server broadcasts to WebSocket clients
3. Droplet 8 sends POST `/verifier/update` → Server caches and broadcasts to WebSocket clients
4. Dashboard JavaScript receives messages and updates UI in real-time

## Notes

- **Secrets**: All secrets live in `.env` file; never commit to version control
- **CORS**: Currently allows all origins (`*`); restrict in production if needed
- **Verifier Cache**: In-memory cache; status is lost on server restart
- **Delete Endpoints**: Removed for safety; droplets cannot be deleted via this API

## Future Enhancements

- Persistent verifier status storage (database)
- Authentication/authorization improvements
- Rate limiting
- WebSocket session management
- Dashboard filtering and search
- Export functionality
- Audit logging
