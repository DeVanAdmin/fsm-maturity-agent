# Step 07 — Custom System Gateway

## What We Built
An Azure API Management gateway + Azure Function handler that accepts work order data from any custom or bespoke FSM system via a standard REST push interface. This is the catch-all connector for clients not running D365, Salesforce, ServiceNow, or IFS.

## Architecture
```
Client FSM System
      │
      │ POST /fma/v1/workorders
      │ Ocp-Apim-Subscription-Key: {key}
      ▼
Azure API Management
  • Validates subscription key
  • Rate limits (100 req/min)
  • Adds correlation ID
  • Routes to backend
      │
      ▼
Azure Function (function_app.py)
  • Validates payload schema
  • Maps to fma_workorder record
  • Upserts to Dataverse
      │
      ▼
Dataverse fma_workorder table
```

## Key Design Decisions
- **Push not pull** — we cannot predict what API a bespoke system exposes. Defining an inbound contract that clients implement is cleaner than trying to build a generic polling adapter.
- **We designed the inbound schema to match our data model** — fields in `workorder-inbound-schema.json` map directly 1:1 to fma_workorder columns. No complex transform layer needed in the handler.
- **Idempotent upserts** — `externalSourceId` is the upsert key. Clients can safely re-send records without creating duplicates.
- **Partial batch success** — the batch endpoint processes all records and reports per-record results. One bad record does not block the rest of the batch.
- **Pure Python validation** — no jsonschema library in the Azure Function. Keeps cold start time low and removes a dependency.
- **Correlation ID on every request** — APIM stamps every request with a correlation ID. The Function logs it. Makes tracing a specific transaction across APIM logs and Function logs straightforward.

## Files
| File | Purpose |
|---|---|
| `api-contract/workorder-inbound-schema.json` | JSON Schema — the inbound payload contract |
| `api-contract/workorder-inbound-example.json` | Example payload for client integration teams |
| `api-contract/integration-guide.md` | Developer guide for client integration teams |
| `apim-policy/inbound-policy.xml` | APIM inbound processing policy |
| `apim-policy/error-policy.xml` | APIM consistent error response policy |
| `handler/function_app.py` | Azure Function — validates, maps, upserts |
| `handler/requirements.txt` | Python dependencies |
| `handler/host.json` | Azure Functions host configuration |

## What a Client Integration Team Needs
1. Their APIM subscription key (provided during onboarding)
2. Their `clientId` GUID (assigned during onboarding)
3. The `integration-guide.md` in this folder
4. A POST to `/fma/v1/workorders` with a valid payload

## Next Step
Step 08 — Azure AI Foundry agent scaffolding: set up the agent project, define the tool schemas, and wire the coordinator agent with its four sub-agents.
