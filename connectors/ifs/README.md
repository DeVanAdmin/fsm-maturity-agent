# IFS Field Service Connector

Ingestion connector that pulls work orders from **IFS Field Service** via its **OData v4** REST API and writes normalized records into our **`fma_workorder`** Dataverse table. Same structural pattern as the D365, Salesforce, and ServiceNow connectors — only the source system differs.

## What this connector does

On each run it:

1. **Polls IFS** with an OData v4 query for `WorkTask` records that are new or changed since the last run.
2. **Normalizes** each raw `WorkTask` into our `fma_workorder` shape using `field-map.json` — mapping IFS `ObjState` values, converting datetimes to UTC, calculating labor minutes, and inferring SLA compliance.
3. **Writes to Dataverse** by upserting each record (via the shared upsert utility), keyed on a composite `WoNo_TaskSeq` id so re-runs never create duplicates.

## Authentication

IFS uses **its own identity provider — the IFS Identity Server** — not Microsoft Entra ID. Two supported paths, selected by `deploymentType`:

- **IFS Cloud (SaaS)** → **OAuth 2.0 Client Credentials** against `{instance}.ifs.cloud/idp/connect/token`, with a scope such as `ifsapp offline_access`. Returns a bearer token.
- **IFS on-premise** → **Basic Auth**. On-premise installations frequently front the OData services with basic authentication rather than the Identity Server, so the connector constructs a `Basic <base64(user:pass)>` header instead.

The `get_auth_headers(config)` router picks the right path from `deploymentType`, so the rest of the connector is **deployment-agnostic** — fetch code never needs to know which auth was used. Writing to Dataverse always uses a **separate** client-credentials token against the target org (via the shared utility).

## `WorkTask` vs `WorkOrder` — why we query `WorkTask`

IFS separates the **work order header** (the `WO` — customer, site, overall job) from the individual **work tasks** (`WorkTask`) that make it up. Each `WorkTask` (`TaskSeq` 1, 2, 3…) is a discrete site visit or activity with its own planned/actual times and state.

We sync at the **`WorkTask`** level (not the header) because per-visit granularity is exactly what the maturity KPIs need — first-time-fix rate, repeat-visit rate, and per-visit labor all require task-level records. Syncing only the header would collapse multiple visits into one row and lose that signal.

## Field mapping (IFS `WorkTask` → `fma_workorder`)

Defined in `field-map.json`.

| IFS source | fma target | Why |
|---|---|---|
| `WoNo` (+`TaskSeq`) | `fma_externalsourceid` | Composite `WoNo_TaskSeq` — unique per task (see below). |
| `WoNo` | `fma_workordernumber` | Human-readable work order number. |
| `ObjState` | `fma_status` | IFS lifecycle states mapped to `Open / In Progress / Completed / Cancelled`. |
| `PlanningDate` | `fma_scheduledstart` | Planned start, UTC. |
| `FinishDate` | `fma_scheduledend` | Planned finish, UTC. |
| `RealTimeSta` | `fma_actualstart` | Actual start, UTC. |
| `RealTimeFinish` | `fma_actualend` | Actual finish, UTC. |
| `EmpNo` | `fma_technicianid` | Assigned employee/technician. |
| `MchNo` | `fma_assetid` | Machine/asset reference. |
| `ActualCost` | `fma_totalcost` | Actual cost, coerced to currency. |
| `ContractId` | `fma_slacompliant` | SLA compliance **inferred** from actual vs planned finish. |

Values not from a direct field mapping:

- **`fma_laborminutes`** — IFS has no labor-minutes field on `WorkTask`, so it is **calculated** from the `RealTimeSta → RealTimeFinish` delta (returns `None` if either is missing).
- **`fma_externalsourceid`** — a **composite** `WoNo_TaskSeq`. One work order (`WoNo`) has many tasks (`TaskSeq`); using `WoNo` alone would cause upsert collisions.
- **`fma_firsttimefix`** — inferred: `ObjState` is `Reported`/`Finished` on the first task (`TaskSeq = 1`).
- `fma_sourcesystem` → IFS static option value `100000003`; `fma_syncedon` → run timestamp (UTC); `fma_client` → the `FMA_CLIENT_ID` being synced.

## Incremental sync — RowVersion vs LastModifiedDate

IFS exposes two candidates for "what changed since last time":

- **`RowVersion`** — a per-row change stamp that advances every time a row is written. **This is what we prefer.** It is set server-side on every change, so it is immune to client clock drift and timezone ambiguity, and it captures *all* modifications (not just those a `LastModifiedDate` trigger happens to update).
- **`LastModifiedDate`** — a datetime column. Workable, but subject to timezone handling and only as reliable as the process that maintains it.

So the connector filters incrementally on **`RowVersion gt {watermark}`**. On the **very first run** there is no watermark yet, so `build_odata_query` falls back to **`SnapShotCreatedDate gt {now − initialSyncDaysBack}`** to bound the initial load to a sensible window. Every query is ordered `$orderby=RowVersion asc` so pages are processed in change order, and pagination uses OData `$skip` (IFS OData v4 uses `$skip`, not `$skiptoken`). The last successful watermark is persisted to `.sync_state.json`, which is **gitignored**.

## IFS Cloud vs on-premise — how auth differs

| | IFS Cloud (SaaS) | IFS on-premise |
|---|---|---|
| Identity | IFS Identity Server (`/idp/connect/token`) | Local basic auth (typically) |
| Flow | OAuth 2.0 client credentials | HTTP Basic |
| Config | `deploymentType: "cloud"`, `auth.tokenUrl/clientId/clientSecret/scope` | `deploymentType: "onpremise"`, `auth.basicAuth.username/password` |
| Env vars | `IFS_TOKEN_URL`, `IFS_CLIENT_ID`, `IFS_CLIENT_SECRET`, `IFS_SCOPE` | `IFS_USERNAME`, `IFS_PASSWORD` |

## Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | What it is |
|---|---|
| `IFS_DEPLOYMENT_TYPE` | `cloud` or `onpremise`. |
| `IFS_TOKEN_URL` | IFS Identity Server token endpoint (cloud). |
| `IFS_CLIENT_ID` / `IFS_CLIENT_SECRET` | OAuth client credentials (cloud). |
| `IFS_SCOPE` | OAuth scope, e.g. `ifsapp offline_access` (cloud). |
| `IFS_USERNAME` / `IFS_PASSWORD` | Basic auth credentials (on-premise only). |
| `IFS_BASE_URL` | IFS OData base URL. |
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
