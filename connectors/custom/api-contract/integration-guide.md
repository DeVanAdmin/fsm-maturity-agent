# Custom FSM Gateway — Client Integration Guide

This guide is for the **integration developer on the client side**. It explains exactly how to push work order data from your custom/bespoke FSM system into the FSM Maturity Agent.

You **push** to us — we do not poll your system. Send a work order whenever one is created or changes.

## Endpoint

```
POST https://{apim-instance}.azure-api.net/fma/v1/workorders          # single work order
POST https://{apim-instance}.azure-api.net/fma/v1/workorders/batch    # up to 100 work orders
```

`{apim-instance}` is provided during onboarding.

## Required headers

| Header | Value |
|---|---|
| `Ocp-Apim-Subscription-Key` | Your APIM subscription key (issued during onboarding). Identifies and authenticates you. |
| `Content-Type` | `application/json` |

## Field contract

**Required** (a request missing any of these is rejected with `400`):

| Field | Type | Notes |
|---|---|---|
| `clientId` | string (UUID) | Your `fma_client` GUID, assigned during onboarding. |
| `externalSourceId` | string (≤100) | The work order ID in *your* system. **This is the upsert key.** |
| `workOrderNumber` | string (≤50) | Human-readable number. |
| `status` | string (enum) | One of the four values below. |
| `scheduledStart` | string (date-time) | ISO 8601 UTC. |

**Optional:**

| Field | Type | Notes |
|---|---|---|
| `scheduledEnd` | string (date-time) | ISO 8601 UTC. |
| `actualStart` | string (date-time) | ISO 8601 UTC. |
| `actualEnd` | string (date-time) | ISO 8601 UTC. |
| `firstTimeFix` | boolean | Resolved on first visit? |
| `visitCount` | integer (≥1) | Total visits. |
| `technicianId` | string (≤100) | Technician ref in your system. |
| `assetId` | string (≤100) | Asset ref in your system. |
| `slaCompliant` | boolean | SLA met? |
| `laborMinutes` | integer (≥0) | Labor time in minutes. |
| `travelMinutes` | integer (≥0) | Travel time in minutes. |
| `totalCost` | number (≥0) | Total cost. |

No other fields are accepted — unknown properties cause a `400` (`additionalProperties: false`).

### Status enum — only these four values

```
Open | In Progress | Completed | Cancelled
```

Any other value (including different casing, e.g. `open`, `COMPLETED`) is rejected. Map your internal statuses to these before sending.

### DateTime format — ISO 8601 UTC only

Send timestamps in UTC with a trailing `Z`:

```
2026-03-15T14:30:00Z
```

Local-time or offset timestamps (e.g. `2026-03-15T14:30:00+05:00`) should be converted to UTC before sending.

## How upserts work (idempotency)

`externalSourceId` is the **idempotency key**. The gateway upserts on it:

- First time we see an `externalSourceId` → a new record is created.
- Any later push with the **same** `externalSourceId` → the existing record is **updated**.

This means you can safely **re-send** a work order — on retry, after a crash, or on every status change — without ever creating duplicates. Send the *current full state* of the work order each time.

## Responses

**Single endpoint — success (`200`):**
```json
{ "status": "ok", "externalSourceId": "WO-2026-000148", "created": false }
```

**Batch endpoint — success (`200`, partial success allowed):**
```json
{
  "processed": 3,
  "succeeded": 2,
  "failed": 1,
  "errors": [
    { "externalSourceId": "WO-2026-000149", "error": "status must be one of: Open, In Progress, Completed, Cancelled" }
  ]
}
```

**Error envelope (all error responses share this shape):**
```json
{ "error": { "code": "validation_error", "message": "clientId is required", "correlationId": "a1b2c3d4-..." } }
```

Always log the `correlationId` — quote it when contacting support so we can trace your exact request across the gateway and handler logs.

### HTTP status codes

| Code | Meaning | What to do |
|---|---|---|
| `200` | Accepted (single) / batch processed (see summary) | For batch, inspect `errors[]` — some records may have failed. |
| `400` | Validation error — malformed JSON, missing required field, bad enum/date, or batch > 100 | Fix the payload; do not retry unchanged. |
| `401` | Authentication error — missing/invalid subscription key | Check `Ocp-Apim-Subscription-Key`. |
| `429` | Rate limit exceeded (see below) | Back off and retry after a short delay. |
| `500` | Internal error on our side | Retry with backoff; if it persists, contact support with the `correlationId`. |

## Rate limits & batching

- **Rate limit:** 100 requests per minute per subscription key.
- **Batch:** send up to **100 records per request** to the `/workorders/batch` endpoint. Prefer batching for backfills and high-volume syncs — one batch of 100 counts as a single request against the rate limit.
- On `429`, back off (exponential is fine) and retry.

## Sample — curl

```bash
curl -X POST "https://YOUR_APIM.azure-api.net/fma/v1/workorders" \
  -H "Ocp-Apim-Subscription-Key: YOUR_SUBSCRIPTION_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "clientId": "3f9a1c2e-7b44-4e8a-9c1d-2a6f5b0e91d7",
        "externalSourceId": "WO-2026-000148",
        "workOrderNumber": "000148",
        "status": "Completed",
        "scheduledStart": "2026-03-15T08:00:00Z",
        "actualEnd": "2026-03-15T10:47:00Z",
        "firstTimeFix": true,
        "laborMinutes": 155
      }'
```

## Sample — Python

```python
import requests

APIM = "https://YOUR_APIM.azure-api.net/fma/v1/workorders"
HEADERS = {
    "Ocp-Apim-Subscription-Key": "YOUR_SUBSCRIPTION_KEY",
    "Content-Type": "application/json",
}

payload = {
    "clientId": "3f9a1c2e-7b44-4e8a-9c1d-2a6f5b0e91d7",
    "externalSourceId": "WO-2026-000148",
    "workOrderNumber": "000148",
    "status": "Completed",
    "scheduledStart": "2026-03-15T08:00:00Z",
    "actualEnd": "2026-03-15T10:47:00Z",
    "firstTimeFix": True,
    "laborMinutes": 155,
}

resp = requests.post(APIM, json=payload, headers=HEADERS, timeout=30)
print(resp.status_code, resp.json())

# Batch: POST a list of up to 100 payloads to the /batch endpoint.
batch = [payload]
resp = requests.post(APIM + "/batch", json=batch, headers=HEADERS, timeout=60)
print(resp.status_code, resp.json())
```
