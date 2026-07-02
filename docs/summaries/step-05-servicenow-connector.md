# Step 05 — ServiceNow FSM Connector

## What We Built
The third ingestion pipeline. Pulls work orders from ServiceNow Field Service Management via the Table API and writes normalized records into `fma_workorder`.

## How It Works
1. Authenticates to ServiceNow via OAuth 2.0 Password Grant (endpoint: `/oauth_token.do`)
2. Queries `wm_order` table with `sys_updated_on > last_sync_time` filter for incremental sync
3. Uses `sysparm_display_value=all` to get both sys_ids and human-readable display values for reference fields
4. Handles offset-based pagination (sysparm_offset increments by page_size until results < page_size)
5. Transforms records — duration strings converted to minutes, reference fields extracted, SLA compliance inferred
6. Upserts to Dataverse via shared utility using `fma_externalsourceid` as alternate key
7. Saves sync timestamp

## Key Differences from Previous Connectors
| Aspect | D365 | Salesforce | ServiceNow |
|---|---|---|---|
| Query language | OData `$filter` | SOQL `WHERE` | sysparm_query |
| Pagination | nextLink URL | nextRecordsUrl | offset integer |
| Reference fields | GUID only | ID only | value + display_value object |
| Duration format | Minutes integer | Minutes integer | "X days HH:MM:SS" string |
| Work order table | msdyn_workorder | WorkOrder | wm_order |

## ServiceNow-Specific Concepts
- **wm_order vs task** — ServiceNow FSM work orders live in `wm_order`, which extends the base `task` table. We query `wm_order` directly to get FSM-specific fields.
- **sysparm_display_value=all** — critical parameter. Without it, reference fields return only GUIDs. With it, they return both the GUID and the human-readable name, giving us useful data for the fma_technician and fma_asset lookups.
- **Duration strings** — ServiceNow stores time durations as "X days HH:MM:SS" strings. The `duration_to_minutes` function converts these to integer minutes for consistent KPI calculation.

## Next Step
Step 06 — IFS connector: uses IFS REST API (OData v4) with similar pattern to D365 but different authentication and table structure.
