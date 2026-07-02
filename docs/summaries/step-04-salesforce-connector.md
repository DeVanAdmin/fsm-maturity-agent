# Step 04 — Salesforce Field Service Connector

## What We Built
The second ingestion pipeline. Pulls work orders from Salesforce Field Service via SOQL query and writes normalized records into `fma_workorder`. Follows the same structural pattern as the D365 connector.

## How It Works
1. Authenticates to Salesforce using OAuth 2.0 Username-Password flow via a Connected App
2. Builds a SOQL query with `LastModifiedDate > last_sync_time` filter for incremental sync
3. Fetches WorkOrder records, following `nextRecordsUrl` for pagination
4. Transforms each record using `field-map.json` — status strings mapped, datetimes normalized, first-time fix inferred
5. Gets a separate Dataverse OAuth token (different auth context from Salesforce)
6. Upserts each record into `fma_workorder` using `fma_externalsourceid` as alternate key
7. Saves sync timestamp

## Key Differences from D365 Connector
| Aspect | D365 | Salesforce |
|---|---|---|
| Query language | OData `$filter` | SOQL `WHERE` clause |
| Auth response | Token only | Token + instance_url |
| Pagination | `@odata.nextLink` | `nextRecordsUrl` |
| First time fix | Native boolean field | Inferred from IsClosed + repeat visits |
| Status values | Numeric option set codes | String labels |

## Shared Utility Introduced
Extracted `get_dataverse_token` and `upsert_record` into `connectors/shared/dataverse_upsert.py`. Every connector writes to the same Dataverse target the same way — shared utility eliminates duplication across all five connectors.

## Key Design Decisions
- **JWT Bearer vs Username-Password** — config uses Username-Password for simplicity. For production, swap to JWT Bearer (server-to-server, no password in env vars). The README documents this upgrade path.
- **First-time fix inference** — Salesforce has no native FTFR field. We infer it from closure status and repeat visit patterns. This inference logic will be refined as we see real data.
- **instance_url is dynamic** — Salesforce returns the correct API base URL at login time. Never hardcode it.

## Next Step
Step 05 — ServiceNow FSM connector: same pattern, uses ServiceNow Table API with BasicAuth or OAuth.
