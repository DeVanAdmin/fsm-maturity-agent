# ServiceNow FSM Connector

Ingestion connector that pulls work orders from **ServiceNow Field Service Management** via the **Table API** and writes normalized records into our **`fma_workorder`** Dataverse table. Same structural pattern as the D365 and Salesforce connectors — only the source system differs.

## What this connector does

On each run it:

1. **Polls ServiceNow** with the Table API for `wm_order` records that are new or changed since the last run.
2. **Normalizes** each raw record into our `fma_workorder` shape using `field-map.json` — mapping state codes, converting datetimes to UTC, turning duration strings into minutes, extracting reference display values, and inferring SLA compliance.
3. **Writes to Dataverse** by upserting each record (via the shared upsert utility), keyed on the originating ServiceNow `sys_id` so re-runs never create duplicates.

## Authentication

**OAuth 2.0 (Password Grant)** for production, with a **BasicAuth** fallback for development:

- When an OAuth `clientId`/`clientSecret` are configured, the connector POSTs to `{instanceUrl}/oauth_token.do` (grant type `password`) and uses the returned bearer token.
- When no OAuth client is configured, it constructs a `Basic <base64(user:pass)>` credential instead. `fetch_workorders` detects which form it received and sets the `Authorization` header accordingly (`Bearer …` vs `Basic …`).
- Create the OAuth client in ServiceNow under **System OAuth → Application Registry**. The service account needs read access to `wm_order`.

Writing to Dataverse uses a **separate** client-credentials token against the target org, obtained via the shared utility.

## `task` vs `wm_order` — why we query `wm_order`

ServiceNow models all work-like records on a base table called **`task`**. Field Service Management work orders live in **`wm_order`**, which *extends* `task`. The base `task` table has the generic columns (number, state, assigned_to, timestamps); `wm_order` adds the FSM-specific columns and semantics.

We query **`wm_order` directly** so that:
- we only get field-service work orders (not incidents, change tasks, or other `task` children), and
- we can select FSM-specific fields alongside the inherited `task` fields in one call.

## Field mapping (ServiceNow `wm_order` → `fma_workorder`)

Defined in `field-map.json`.

| ServiceNow source | fma target | Why |
|---|---|---|
| `sys_id` | `fma_externalsourceid` | Originating ID — upsert alternate key and traceability. |
| `number` | `fma_workordernumber` | Human-readable number (e.g. `WO0001001`). |
| `state` | `fma_status` | Numeric state codes mapped to `Open / In Progress / Completed / Cancelled`. |
| `opened_at` | `fma_scheduledstart` | Opened time, normalized to UTC. |
| `sla_due` | `fma_scheduledend` | SLA due time, UTC. |
| `work_start` | `fma_actualstart` | Actual start, UTC. |
| `work_end` | `fma_actualend` | Actual end, UTC. |
| `total_effort` | `fma_laborminutes` | Duration string converted to whole minutes. |
| `cmdb_ci` | `fma_assetid` | Configuration item (asset) — **display value** extracted. |
| `assigned_to` | `fma_technicianid` | Assigned technician — **display value** extracted. |
| `upon_approval` | `fma_slacompliant` | SLA compliance **inferred** from `work_end` vs `sla_due`. |

Connector-set values: `fma_sourcesystem` → ServiceNow static option value `100000002`; `fma_syncedon` → run timestamp (UTC); `fma_client` → the `FMA_CLIENT_ID` being synced.

## Incremental sync

The connector keeps a local `.sync_state.json` with the last successful run timestamp:

- **First run** (no state file): pulls everything modified in the last `initialSyncDaysBack` days (default 90).
- **Every later run**: `build_query_params` sets `sysparm_query` to `sys_updated_on>{ts}^ORDERBYsys_updated_on` (ServiceNow datetime format `YYYY-MM-DD HH:MM:SS`, UTC) so only changed records return, in a stable order for offset paging.
- After a successful run the new timestamp is written back. The state file is **gitignored**.

Two ServiceNow-specific details make the data usable:

- **`sysparm_display_value=all`** — without it, reference fields (`assigned_to`, `cmdb_ci`) return only opaque `sys_id` GUIDs. With it, each field returns both the `value` (GUID) and a `display_value` (human-readable name), so we can store meaningful technician/asset identifiers.
- **Duration strings** — `total_effort` comes back as an `X days HH:MM:SS` style string; `duration_to_minutes` converts it to integer minutes for consistent KPI math.

## Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | What it is |
|---|---|
| `SN_INSTANCE_URL` | ServiceNow instance base URL (no trailing slash). |
| `SN_CLIENT_ID` | OAuth application-registry client ID. |
| `SN_CLIENT_SECRET` | OAuth client secret. |
| `SN_USERNAME` | Service-account username (read access to `wm_order`). |
| `SN_PASSWORD` | Service-account password. |
| `FMA_TARGET_DATAVERSE_URL` | Base URL of the target Dataverse org. |
| `FMA_DATAVERSE_TENANT_ID` | Entra tenant for Dataverse writes. |
| `FMA_DATAVERSE_CLIENT_ID` | App (client) ID for Dataverse writes. |
| `FMA_DATAVERSE_CLIENT_SECRET` | Client secret for Dataverse writes. |
| `FMA_CLIENT_ID` | GUID of the `fma_client` these work orders belong to. |

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in your values
python connector.py
```
