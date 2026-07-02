# Salesforce Field Service Connector

Ingestion connector that pulls work orders from **Salesforce Field Service** and writes normalized records into our **`fma_workorder`** Dataverse table. It follows the same structural pattern as the D365 connector — only the source system differs.

## What this connector does

On each run it:

1. **Polls Salesforce Field Service** with a SOQL query for `WorkOrder` records that are new or changed since the last run.
2. **Normalizes** each raw `WorkOrder` into our `fma_workorder` shape using `field-map.json` — translating status labels, converting datetimes to UTC, inferring first-time-fix, and stamping the source system.
3. **Writes to Dataverse** by upserting each record into the target `fma_workorder` table (via the shared upsert utility), keyed on the originating Salesforce ID so re-runs never create duplicates.

## Authentication

**OAuth 2.0 Connected App.** Two supported flows:

- **Username-Password flow** (what `config.json` and `.env.example` are set up for). A Connected App plus a service-account username and password-with-security-token are exchanged for an access token at `{loginUrl}/services/oauth2/token`. Simple to stand up; the tradeoff is a password living in the environment.
- **JWT Bearer flow** (recommended for production, server-to-server). The Connected App is configured with a certificate and the connector signs a JWT assertion instead of sending a password — no password in env vars at all. Swapping to it only changes `get_access_token`; the rest of the pipeline is unchanged.

Salesforce returns both an `access_token` **and an `instance_url`** at login. The instance URL is the per-org API base and is **never hardcoded** — every subsequent call uses the value returned at login.

Writing to Dataverse uses a **separate** OAuth token (client-credentials against the target org) — a different auth context from Salesforce, obtained via the shared utility.

## Field mapping (Salesforce `WorkOrder` → `fma_workorder`)

Defined in `field-map.json`.

| Salesforce source | fma target | Why |
|---|---|---|
| `Id` | `fma_externalsourceid` | Originating ID — upsert alternate key and traceability. |
| `WorkOrderNumber` | `fma_workordernumber` | Human-readable number. |
| `Status` | `fma_status` | Salesforce status labels mapped to `Open / In Progress / Completed / Cancelled`. |
| `StartDate` | `fma_scheduledstart` | Scheduled start, normalized to UTC. |
| `EndDate` | `fma_scheduledend` | Scheduled end, normalized to UTC. |
| `ActualStartTime` | `fma_actualstart` | Actual start, UTC. |
| `ActualEndTime` | `fma_actualend` | Actual end, UTC. |
| `Duration` | `fma_laborminutes` | Labor duration. |
| `AssetId` | `fma_assetid` | Serviced asset reference. |
| `OwnerId` | `fma_technicianid` | Owning user/technician reference. |
| `TotalPrice` | `fma_totalcost` | Total cost, coerced to currency. |
| `IsClosed` | `fma_firsttimefix` | **Inferred** — Salesforce has no native FTFR field (see below). |

Connector-set values: `fma_sourcesystem` → Salesforce static option value `100000001`; `fma_syncedon` → run timestamp (UTC); `fma_client` → the `FMA_CLIENT_ID` being synced.

### First-time-fix inference

Salesforce does not expose a native first-time-fix boolean, so `infer_first_time_fix` derives it: not-closed → `False`; closed with repeat-visit data → `True` only if a single visit; closed with no repeat data available → `True` as the best available inference. This heuristic will be refined against real service-appointment data.

## Incremental sync

The connector keeps a local `.sync_state.json` with the last successful run timestamp:

- **First run** (no state file): pulls everything modified in the last `initialSyncDaysBack` days (default 90).
- **Every later run**: `build_soql_query` adds `WHERE LastModifiedDate > {ts}` (Salesforce ISO-8601 `Z` format) so only changed records return, ordered `LastModifiedDate ASC`, capped at `pageSize`.
- After a successful run the new timestamp is written back. The state file is **gitignored**.

## Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | What it is |
|---|---|
| `SF_LOGIN_URL` | Salesforce login URL (`https://login.salesforce.com`, or `test.salesforce.com` for sandboxes). |
| `SF_CLIENT_ID` | Connected App consumer key. |
| `SF_CLIENT_SECRET` | Connected App consumer secret. |
| `SF_USERNAME` | Service-account username. |
| `SF_PASSWORD_TOKEN` | Service-account password **+** security token, concatenated (no space). |
| `SF_API_VERSION` | Salesforce REST API version (e.g. `v59.0`). |
| `FMA_TARGET_DATAVERSE_URL` | Base URL of the target Dataverse org. |
| `FMA_DATAVERSE_TENANT_ID` | Entra tenant for the Dataverse app registration. |
| `FMA_DATAVERSE_CLIENT_ID` | App (client) ID for Dataverse writes. |
| `FMA_DATAVERSE_CLIENT_SECRET` | Client secret for Dataverse writes. |
| `FMA_CLIENT_ID` | GUID of the `fma_client` these work orders belong to. |

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in your values
python connector.py
```
