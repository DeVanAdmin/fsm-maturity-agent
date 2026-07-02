# Dataverse Solution — FSM Maturity Agent Data Layer

This folder holds the **schema documentation and configuration** for the FSM Maturity Agent Dataverse solution. Nothing here is deployed yet — these files define the tables, columns, choices, and relationships that will be used to build the solution in Dataverse.

- `publisher.json` — publisher definition (prefix `fma`, option value prefix `10000`)
- `tables/*.json` — one file per table, with full column definitions
- `relationships.json` — every table-to-table relationship
- `README.md` — this document

## The publisher prefix `fma`

Every table and column in this solution is prefixed with `fma` (short for **FSM Maturity Agent**). In Dataverse, a publisher prefix namespaces all customizations to a solution. It matters because:

- It keeps our tables and columns **isolated** from other solutions installed in the same environment — there is no risk of colliding with `msdyn_`, `salesforce_`, or another ISV's schema.
- It makes ownership obvious: anything named `fma_*` belongs to this solution.
- Option set values are also namespaced via the option value prefix `10000`, so our choice values never clash with other publishers' choices.

## What each table is for (plain English)

| Table | What it holds |
|---|---|
| **fma_client** | The customer organization. This is the top-level record — every other table links back to a client. Holds industry vertical, which FSM platforms they run, and subscription status. |
| **fma_workorder** | A single job/work order, normalized into one shape no matter which platform it came from. Holds timing, first-time-fix, visit count, SLA compliance, labor/travel time, and cost. |
| **fma_technician** | The field technicians, with their skills (as a JSON array), region, and active status. |
| **fma_asset** | The equipment being serviced, with install/service dates, uptime, and maintenance strategy (reactive/preventive/predictive). |
| **fma_kpisnapshot** | Calculated KPI values for a client over a time period (FTFR, MTTR, utilization, SLA compliance, etc.). This is the bridge between raw work orders and maturity scores. |
| **fma_maturityscore** | The output of a scoring run: four dimension scores plus a composite score (1.0–5.0) and a maturity level (Basic → Optimized). |
| **fma_benchmark** | Industry peer benchmarks by vertical and KPI (peer average, best-in-class, minimum acceptable). Seeded once, refreshed periodically. |
| **fma_recommendation** | Agent-generated next-best actions, each tied to the KPI that triggered it and how far below benchmark it fell, with a human advisor review workflow. |
| **fma_alert** | Fired threshold alerts and the Level 3 autonomous action audit trail. One table serves both, distinguished by `fma_alerttype`. |

## The data flow

```
FSM Platform (D365 / Salesforce / ServiceNow / IFS / Custom)
        │
        ▼
  fma_workorder        raw jobs land here, normalized to one shape
        │
        ▼
  fma_kpisnapshot      KPIs are calculated per client per period
        │
        ▼
  fma_maturityscore    dimension + composite scores are written from the snapshot
        │
        ├────────────► fma_recommendation   next-best actions
        └────────────► fma_alert            threshold breaches & autonomous actions
```

1. **Work orders come in.** Each connector pulls jobs from its platform and writes normalized rows into `fma_workorder`.
2. **KPI snapshots are calculated.** The scoring engine aggregates work orders (and asset/technician data) for a client over a period and writes a row into `fma_kpisnapshot`.
3. **Maturity scores are written.** The agent scores the snapshot across four dimensions and writes a row into `fma_maturityscore`, linked back to the snapshot it came from.
4. **Recommendations and alerts are generated.** Based on the scores and benchmark gaps, the agent produces `fma_recommendation` rows and, where thresholds are breached or autonomous action is taken, `fma_alert` rows.

## Traceability — external source IDs are always preserved

Every ingested table (`fma_workorder`, `fma_technician`, `fma_asset`) keeps the originating system's identifier in **`fma_externalsourceid`**, and work orders additionally record **`fma_sourcesystem`**. This means any normalized record can always be traced back to its exact record in D365, Salesforce, ServiceNow, IFS, or a custom source — nothing is orphaned from its origin.
