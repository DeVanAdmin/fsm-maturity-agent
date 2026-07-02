# FSM Maturity Agent

An always-on, AI-driven Field Service Management maturity monitoring system. Continuously ingests live FSM data from multiple platforms, scores operational maturity across four dimensions, benchmarks against industry peers, and surfaces proactive improvement recommendations — delivered as a model-driven Power App with a custom canvas page hosting four PCF controls, powered by Azure AI Foundry.

## Architecture

Key components:

- **Azure AI Foundry** — agent runtime
- **Dataverse** — data layer
- **Azure Data Lake** — raw ingestion buffer
- **Power Apps Model-Driven App + Custom Canvas Page** — UI shell
- **Four PCF controls** — MaturityScorecard, KPIBenchmarkView, RecommendationsQueue, AlertAuditFeed
- **Power Automate + Azure Logic Apps** — orchestration and connectors
- **Azure Functions (Python)** — scoring engine
- **GitHub Actions** — CI/CD

## Connectors

Data reaches the `fma_workorder` table through five source connectors. Four are **pull-based** — we poll the source system on a schedule and pull work orders out of it. One is **push-based** — the client's system sends work orders to us.

| Connector | Model | How data moves |
|---|---|---|
| D365 Field Service | Pull | We poll the Dataverse Web API |
| Salesforce FSL | Pull | We poll the Salesforce REST/SOQL API |
| ServiceNow FSM | Pull | We poll the ServiceNow Table API |
| IFS | Pull | We poll the IFS OData v4 API |
| Custom | **Push** | The client's bespoke system POSTs to our APIM gateway |

The **Custom** connector is a push-based gateway (Azure API Management + an Azure Function): we cannot predict what API a homegrown FSM system exposes, so instead of polling it we define a standard inbound contract and the client pushes data to us. The other four are pull-based because those platforms expose well-known APIs we can query directly.

## Build Steps

- `01` — Repo setup + folder structure
- `02` — Dataverse data model
- `03` — D365 Field Service connector
- `04` — Salesforce FSL connector
- `05` — ServiceNow FSM connector
- `06` — IFS connector
- `07` — Custom system gateway
- `08` — Azure AI Foundry agent scaffolding
- `09` — Scoring engine (Python Azure Function)
- `10` — Maturity scoring tool wired into Foundry agent
- `11` — Benchmark database + compare tool
- `12` — Recommendation rules layer
- `13` — Recommendation LLM layer
- `14` — PCF 1: Maturity Scorecard control
- `15` — PCF 2: KPI & Benchmark View control
- `16` — PCF 3: Recommendations Queue control
- `17` — PCF 4: Alert & Audit Feed control
- `18` — Canvas page shell
- `19` — Alert pipeline (Power Automate → Teams)
- `20` — Level 3 action layer + guardrails
- `21` — Audit trail + override UI
- `22` — End-to-end integration test harness
- `23` — Deployment pipeline (GitHub Actions)

## Getting Started

Setup instructions will be added as each step is completed.

## Contributing

Contribution guidelines will be added.
