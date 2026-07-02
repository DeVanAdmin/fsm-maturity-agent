# Step 06 — IFS Field Service Connector

## What We Built
The fourth ingestion pipeline. Pulls work orders from IFS Field Service via OData v4 and writes normalized records into `fma_workorder`. Supports both IFS Cloud (OAuth) and IFS on-premise (Basic Auth) deployments.

## How It Works
1. Determines deployment type from config (cloud vs on-premise) and gets correct auth headers
2. Builds OData v4 query with `RowVersion gt {last_row_version}` filter for incremental sync
3. Fetches WorkTask records using `$skip`-based pagination
4. Transforms records — calculates labor minutes from actual start/finish delta, infers SLA compliance from scheduled vs actual finish, builds composite external source ID from WoNo + TaskSeq
5. Upserts to Dataverse via shared utility
6. Saves sync timestamp

## Key Differences from Previous Connectors
| Aspect | D365 | Salesforce | ServiceNow | IFS |
|---|---|---|---|---|
| Protocol | OData v4 | REST/SOQL | Table API | OData v4 |
| Auth | Azure AD OAuth | Connected App | IFS IdP OAuth or BasicAuth | SF OAuth |
| Work order entity | msdyn_workorder | WorkOrder | wm_order | WorkTask |
| Pagination | nextLink URL | nextRecordsUrl | sysparm_offset | $skip integer |
| Labor minutes | Direct field | Direct field | Duration string | Calculated from delta |
| Incremental field | modifiedon | LastModifiedDate | sys_updated_on | RowVersion |

## IFS-Specific Concepts
- **WorkTask vs WorkOrder** — IFS separates the work order header (WO) from individual tasks (WorkTask). Each task is a discrete site visit or activity. We sync at the WorkTask level to capture per-visit data needed for FTFR calculation.
- **RowVersion for incremental sync** — IFS RowVersion is a monotonically increasing integer stamp updated on every record change. More reliable than datetime for incremental sync because it is immune to timezone issues and clock drift.
- **Composite external source ID** — `WoNo + "_" + TaskSeq` ensures uniqueness. A single work order (WoNo) can have multiple tasks (TaskSeq 1, 2, 3...). Using WoNo alone would cause upsert collisions.
- **Dual auth support** — IFS Cloud uses the IFS Identity Server (not Azure AD). On-premise IFS uses Basic Auth. The `get_auth_headers` router function abstracts this so the rest of the connector is deployment-agnostic.

## Next Step
Step 07 — Custom system gateway: Azure API Management gateway that provides a consistent REST interface for any custom or bespoke FSM system not covered by the four platform connectors.
