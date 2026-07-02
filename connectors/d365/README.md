# D365 Field Service Connector

Ingestion connector that pulls work orders from **Dynamics 365 Field Service** and writes normalized records into our **`fma_workorder`** Dataverse table.

## What this connector does

On each run it:

1. **Polls D365 Field Service** for work orders that are new or changed since the last run.
2. **Normalizes** each raw D365 `msdyn_workorder` into our `fma_workorder` shape using `field-map.json` — translating status codes, converting datetimes to UTC, and stamping the source system.
3. **Writes to Dataverse** by upserting each record into the target `fma_workorder` table, keyed on the originating D365 ID so re-runs never create duplicates.

It is designed to run on a schedule (Azure Function timer, Logic App, or cron). Every run is incremental — it only pulls what changed.

## Authentication

**OAuth 2.0 Client Credentials flow** (service-to-service, no interactive user login):

- An **App Registration** in Microsoft Entra ID (Azure AD) is granted access to the D365 org.
- The connector exchanges `tenantId` + `clientId` + `clientSecret` for an access token at the Microsoft identity platform token endpoint (`https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`), using the D365 org URL as the scope (`{resource}/.default`).
- The same token is reused for every API call within a single sync run.
- In production the client secret is pulled from **Azure Key Vault**, never stored in the repo. Locally it comes from an untracked `.env` file.

## Field mapping (D365 → `fma_workorder`)

Defined in `field-map.json`. Summary of what is mapped and why:

| D365 source | fma target | Why |
|---|---|---|
| `msdyn_workorderid` | `fma_externalsourceid` | Preserves the originating ID — the upsert alternate key and full traceability back to D365. |
| `msdyn_name` | `fma_workordernumber` | Human-readable work order number. |
| `msdyn_systemstatus` | `fma_status` | D365 status option-set codes are translated to our normalized `Open / In Progress / Completed / Cancelled`. |
| `msdyn_timewindowstart` | `fma_scheduledstart` | Scheduled start, normalized to UTC. |
| `msdyn_timewindowend` | `fma_scheduledend` | Scheduled end, normalized to UTC. |
| `msdyn_actualstarttime` | `fma_actualstart` | Actual start, normalized to UTC. |
| `msdyn_actualduration` | `fma_laborminutes` | Actual labor duration in minutes. |
| `_msdyn_primaryincidenttype_value` | `fma_assetid` | Reference to the serviced asset/incident type. |
| `_ownerid_value` | `fma_technicianid` | Owning user/technician reference. |
| `msdyn_totalcost` | `fma_totalcost` | Total cost, coerced to a currency value. |

Two values are set by the connector rather than mapped from a source field:

- `fma_sourcesystem` → the D365 static option value (`100000000`), so every row is labeled with where it came from.
- `fma_syncedon` → the UTC timestamp of the sync run.

And `fma_client` is set to the `FMA_CLIENT_ID` the connector is run for.

## Incremental sync

We never pull the whole dataset on every run. The connector keeps a local `.sync_state.json` file holding the timestamp of the last successful run:

- **First run** (no state file): pulls everything modified in the last `initialSyncDaysBack` days (default 90).
- **Every later run**: adds an OData `$filter` of `modifiedon gt {last_sync}` so D365 only returns records changed since we last succeeded.
- After a successful run the new timestamp is written back to `.sync_state.json`.

Because `modifiedon` covers both new and updated records, a single filter captures inserts and edits alike. The state file is **gitignored** — it is local runtime state, not source.

## Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | What it is |
|---|---|
| `D365_TENANT_ID` | Entra ID tenant that owns the app registration. |
| `D365_CLIENT_ID` | Application (client) ID of the app registration. |
| `D365_CLIENT_SECRET` | Client secret value (Key Vault in production). |
| `D365_ORG_URL` | Base URL of the D365 Field Service org to pull from. |
| `FMA_TARGET_DATAVERSE_URL` | Base URL of the target Dataverse org to write to. |
| `FMA_CLIENT_ID` | GUID of the `fma_client` these work orders belong to. |

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in your values
python connector.py
```
