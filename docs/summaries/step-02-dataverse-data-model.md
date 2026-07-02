# Step 02 — Dataverse Data Model

## What We Built
Defined the complete Dataverse schema for the FSM Maturity Agent unified data layer. Ten tables covering the full data flow from raw work order ingestion through KPI calculation, maturity scoring, benchmarking, recommendations, and alerts.

## Tables Created
| Table | Purpose |
|---|---|
| fma_client | Top-level org record — everything links here |
| fma_workorder | Unified work order from any FSM platform |
| fma_technician | Technician records with skills and region |
| fma_asset | Equipment/asset records with maintenance history |
| fma_kpisnapshot | Time-series KPI values per client per period |
| fma_maturityscore | Dimension scores + composite score per scoring run |
| fma_benchmark | Industry peer benchmarks by vertical and KPI |
| fma_recommendation | Agent-generated next-best actions with advisor workflow |
| fma_alert | Threshold breaches and Level 3 autonomous action log |

## Key Design Decisions
- Publisher prefix `fma` on every column and table — keeps our solution isolated from other solutions in the same environment
- Every work order preserves `fma_externalsourceid` and `fma_sourcesystem` — records are always traceable back to D365, Salesforce, ServiceNow, IFS, or custom origin
- `fma_kpisnapshot` is the bridge between raw work order data and maturity scores — the scoring engine reads snapshots, not raw work orders
- `fma_maturityscore` stores both dimension scores (1–5) and a composite weighted score — the PCF scorecard control reads directly from this table
- `fma_alert` handles both Level 2 (threshold notifications) and Level 3 (autonomous action audit trail) in one table, distinguished by `fma_alerttype`

## Data Flow
FSM Platform → fma_workorder → fma_kpisnapshot → fma_maturityscore → fma_recommendation + fma_alert

## Next Step
Step 03 — D365 Field Service connector: build the first ingestion pipeline that pulls work orders from Dynamics 365 Field Service and writes normalized records into fma_workorder.
