# Step 03 — D365 Field Service Connector

## What We Built
The first live ingestion pipeline. Pulls work orders from Dynamics 365 Field Service and writes normalized records into the `fma_workorder` Dataverse table.

## How It Works
1. Authenticates to D365 using OAuth 2.0 client credentials (app registration — no user login required)
2. Queries `msdyn_workorders` via the D365 OData API, filtering by `modifiedon > last_sync_time` so only changed records are pulled
3. Transforms each raw D365 record using `field-map.json` — status codes are translated, datetimes normalized to UTC, source system stamped
4. Upserts each record into `fma_workorder` using `fma_externalsourceid` as the alternate key — re-running never creates duplicates
5. Saves the sync timestamp so the next run knows where to resume

## Key Files
| File | Purpose |
|---|---|
| `connector.py` | Main Python module — auth, fetch, transform, upsert, sync state |
| `config.json` | Configuration schema with placeholders (no real secrets) |
| `field-map.json` | D365 → fma_workorder field mapping with transform rules |
| `requirements.txt` | Python dependencies |
| `tests/test_transform.py` | Unit tests for field transformation logic |
| `tests/test_sync.py` | Unit tests for sync orchestration |
| `.env.example` | Environment variable template |

## Key Design Decisions
- **Incremental sync via `modifiedon`** — we never pull the full dataset on every run. First run pulls 90 days back; every subsequent run picks up from the last successful sync timestamp.
- **Upsert via alternate key** — `fma_externalsourceid` + `fma_sourcesystem` uniquely identifies a work order from D365. PATCH with alternate key means the connector is idempotent — safe to re-run without duplicating data.
- **No secrets in repo** — all credentials come from environment variables. In production these will come from Azure Key Vault.
- **Sync state file is gitignored** — `.sync_state.json` lives locally and is never committed.

## To Run This Connector
1. Copy `.env.example` to `.env` and fill in your D365 credentials
2. Create an App Registration in Azure AD with Dynamics CRM user_impersonation permission
3. `pip install -r requirements.txt`
4. `python connector.py`

## Next Step
Step 04 — Salesforce FSL connector: same pattern, different source system.
