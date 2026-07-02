# Custom FSM Gateway (push-based)

The catch-all ingestion path for any client running a homegrown or niche FSM system not covered by the D365, Salesforce, ServiceNow, or IFS connectors.

## How this differs from the other four connectors — push vs pull

The other four connectors are **pull-based**: *we* poll the client's system on a schedule and pull work orders out of it.

This connector is **push-based**: *the client's system sends work orders to us*. We expose an HTTPS endpoint; the client POSTs to it whenever a work order is created or changes.

```
Pull (D365 / Salesforce / ServiceNow / IFS):   FMA  ──poll──▶  client system
Push (Custom):                                  client system  ──POST──▶  FMA gateway
```

### Why push for custom systems

We cannot predict what API — if any — a bespoke, in-house FSM system exposes. Building a generic polling adapter for "some unknown API" is not feasible. So instead of adapting to their system, we **define one standard inbound contract** and ask the client to send data that matches it. We control the shape; they implement the push. This is simpler, more reliable, and works for literally any source system that can make an HTTPS call.

## The two components

1. **The API contract** (`api-contract/`) — what the client must send.
   - `workorder-inbound-schema.json` — the JSON Schema (draft-07) contract.
   - `workorder-inbound-example.json` — a fully-populated example.
   - `integration-guide.md` — the developer guide handed to the client's integration team.

2. **The gateway** — how we receive, validate, and route it.
   - `apim-policy/` — Azure API Management policies (`inbound-policy.xml`, `error-policy.xml`).
   - `handler/` — the backend **Azure Function** (`function_app.py`) that validates, maps, and writes to Dataverse.

```
Client FSM System
   │  POST /fma/v1/workorders   (Ocp-Apim-Subscription-Key: {key})
   ▼
Azure API Management  ── validates subscription key, checks content-type,
   │                     stamps correlation id, rate-limits, routes to backend
   ▼
Azure Function (handler/function_app.py)
   │  ── validates payload, maps to fma_workorder, upserts to Dataverse
   ▼
Dataverse  fma_workorder
```

## Inbound authentication

Requests authenticate with an **APIM subscription key**. The client includes it on every request as the `Ocp-Apim-Subscription-Key` header. APIM validates the key **natively** (the API requires a subscription) and rejects missing/invalid keys with `401` before our policy logic runs. Each client gets their own subscription key at onboarding, which also drives per-client rate limiting.

## How the gateway validates, normalizes, and writes

1. **APIM inbound policy** — confirms `Content-Type: application/json`, stamps an `X-Correlation-Id` for tracing, records the Custom source-system value (`100000004`), rate-limits to 100 req/min per key, and routes to the Function.
2. **Function validation** — `validate_payload` does pure-Python checks (required fields, `status` enum, ISO-8601 datetimes, UUID `clientId`). No `jsonschema` dependency, so cold starts stay fast.
3. **Mapping** — `map_to_fma_record` maps the payload 1:1 to `fma_workorder` (the inbound schema was designed to match our data model), stamping `fma_sourcesystem = 100000004` and `fma_syncedon`.
4. **Write** — upserts to `fma_workorder` through the shared Dataverse utility, keyed on `fma_externalsourceid` (idempotent — re-sends update, never duplicate).
5. **Errors** — every failure returns the consistent envelope `{ "error": { code, message, correlationId } }`; the APIM error policy guarantees this shape even for APIM-level failures.

Single and batch endpoints are provided; the batch endpoint accepts up to 100 records and allows **partial success** (one bad record does not fail the batch).

## What a client integration team implements

On *their* side they need to:

1. Obtain their **APIM subscription key** and **`clientId` GUID** (both issued at onboarding).
2. Map their internal statuses to our four values (`Open`, `In Progress`, `Completed`, `Cancelled`).
3. Convert timestamps to **ISO 8601 UTC**.
4. POST each new/changed work order to `/fma/v1/workorders` (or batches to `/fma/v1/workorders/batch`) with the two required headers.
5. Handle the response codes and retry on `429`/`500` with backoff.

Everything they need is in `api-contract/integration-guide.md`.

## Environment variables

For local development of the Function handler, copy `.env.example` to a `local.settings.json` (see `handler/local.settings.json.example`) and populate:

| Variable | What it is |
|---|---|
| `FMA_DATAVERSE_URL` | Base URL of the target Dataverse org. |
| `FMA_DATAVERSE_TENANT_ID` | Entra tenant for Dataverse writes. |
| `FMA_DATAVERSE_CLIENT_ID` | App (client) ID for Dataverse writes. |
| `FMA_DATAVERSE_CLIENT_SECRET` | Client secret for Dataverse writes. |
| `APIM_SUBSCRIPTION_KEY` | A subscription key, for locally testing calls through APIM. |

In production the Dataverse credentials come from **Azure Key Vault** (Function App settings via Key Vault references) — never from a committed file. The real `local.settings.json` is gitignored.

## Tests

```bash
pip install -r handler/requirements.txt   # provides azure-functions
pytest connectors/custom/tests -q
```
