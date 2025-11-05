#!/usr/bin/env bash
# control-smoke.sh — tests control endpoints on drop7.fullpotential.ai
# Usage: ADMIN_TOKEN="your-token" bash control-smoke.sh

set -euo pipefail

DOMAIN="https://drop7.fullpotential.ai"
ADMIN_TOKEN="${ADMIN_TOKEN:-}"
[[ -z "$ADMIN_TOKEN" ]] && { echo "ERROR: Set ADMIN_TOKEN env var"; exit 1; }

echo "Fetching droplets..."
LIST_JSON="$(curl -s "${DOMAIN}/list")"
COUNT="$(echo "$LIST_JSON" | jq -r '.count')"
[[ "$COUNT" == "null" || -z "$COUNT" ]] && { echo "ERROR: Could not read count"; echo "$LIST_JSON"; exit 1; }

DROPLET_ID="$(echo "$LIST_JSON" | jq -r '.droplets[0].droplet_id')"
[[ -z "$DROPLET_ID" || "$DROPLET_ID" == "null" ]] && { echo "ERROR: No droplets found"; exit 1; }
echo "Testing droplet ID: $DROPLET_ID"

echo "Rebooting..."
curl -s -X POST "${DOMAIN}/power/${DROPLET_ID}?action=reboot" -H "x_admin_token: ${ADMIN_TOKEN}" | jq .

echo "Powering off (caution)..."
curl -s -X POST "${DOMAIN}/power/${DROPLET_ID}?action=power_off" -H "x_admin_token: ${ADMIN_TOKEN}" | jq .

echo "Powering on..."
curl -s -X POST "${DOMAIN}/power/${DROPLET_ID}?action=power_on" -H "x_admin_token: ${ADMIN_TOKEN}" | jq .

echo "Done."

