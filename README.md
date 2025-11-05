# Droplet Manager Prototype (FastAPI)

A 5-hour proof-of-concept that:
- Installs doctl alternative via DigitalOcean REST API
- Exposes REST endpoints: `/` (health), `/register` (POST), `/list` (GET)
- Logs operations to Airtable table `events` with fields: `droplet_id`, `name`, `ip`, `status`, `created`

## Prerequisites
- Docker installed on the droplet
- `.env` file in project root (not committed):
```
DO_TOKEN=your_digitalocean_token
AIRTABLE_BASE_ID=QxZ9bNo0NlHnDU
AIRTABLE_API_KEY=your_airtable_key
AIRTABLE_TABLE=events
```

## Build & Run
```bash
docker build -t droplet-manager .
docker run -d -p 80:8000 --env-file .env --name droplet-manager droplet-manager
```

## Endpoints
- Health
  ```
  GET /
  Response: {"status":"ok"}
  ```
- Register
  ```
  POST /register
  Header: Content-Type: application/json
  Body: {"droplet_id":123,"name":"test","ip":"1.2.3.4","created":"optional-ISO8601"}
  Response: {"ok": true, "received": {...}}
  Airtable: adds a row with status="registered"
  ```
- List Droplets
  ```
  GET /list
  Response: {"count": N, "droplets": [{droplet_id, name, ip, status, created}]}
  Airtable: logs each droplet as a row
  ```

# Droplet Manager Prototype (FastAPI)

Clean prototype with:
- Status endpoints: `/` (health), `/register` (POST), `/list` (GET)
- Control endpoints: `/power/{droplet_id}` (POST), `/destroy/{droplet_id}` (DELETE)
- Airtable logging: `events` table with `droplet_id`, `name`, `ip`, `status`, `created`

## Prerequisites
- Docker on the server
- `.env` in project root (not committed):


## Smoke Tests
```bash
curl http://<IP>/
curl -X POST http://<IP>/register -H "Content-Type: application/json" -d '{"droplet_id":123,"name":"test","ip":"1.2.3.4"}'
curl http://<IP>/list
```

## Troubleshooting
- Check logs: `docker logs droplet-manager --tail 100`
- Confirm envs: `docker exec -it droplet-manager sh -c 'env | grep -E "DO_TOKEN|AIRTABLE|TABLE"'`
- Verify Airtable table has columns: droplet_id, name, ip, status, created

## Notes
- Secrets live only in `.env`; do not commit
- Future sprints: auth, dashboards, scaling, SSH key rotation, CI/CD
