# Step 01 — Repo Setup & Folder Structure

## What We Built
Initialized the GitHub repository and established the complete folder structure for the FSM Maturity Agent project.

## Folder Structure
- `.github/workflows/` — GitHub Actions CI/CD pipelines (populated in Step 23)
- `dataverse/solution/` — Dataverse solution files and table schemas (Step 02)
- `connectors/` — One subfolder per FSM platform (D365, Salesforce, ServiceNow, IFS, Custom)
- `agent/foundry/` — Azure AI Foundry agent configuration and system prompts
- `agent/tools/` — Agent tool definitions (function calling schemas)
- `functions/scoring-engine/` — Python Azure Function for KPI calculation and maturity scoring
- `pcf/MaturityScorecard/` — PCF Control 1: composite score and four-dimension breakdown
- `pcf/KPIBenchmarkView/` — PCF Control 2: KPI values vs industry peer benchmarks
- `pcf/RecommendationsQueue/` — PCF Control 3: agent recommendations and advisor actions
- `pcf/AlertAuditFeed/` — PCF Control 4: alerts, autonomous actions, and override surface
- `canvas/MaturityDashboard/` — Custom canvas page that hosts all four PCF controls
- `docs/summaries/` — Step-by-step build summaries (this folder)

## Key Decisions
- Four PCF controls are embedded inside a custom canvas page, not directly on model-driven app forms. The canvas page acts as the layout shell; each PCF control is a self-contained focused component.
- Canvas page lives in its own folder separate from PCF controls so the shell and the components can be versioned independently.
- `local.settings.json` is gitignored — Azure Function secrets never touch the repo.

## What This Enables
Every subsequent build step has a defined home in the repo. Commit history will map directly to the 23-step build plan, making the project auditable and easy to hand off.

## Next Step
Step 02 — Dataverse data model: define and create all Dataverse tables, columns, and relationships that form the unified data layer.
