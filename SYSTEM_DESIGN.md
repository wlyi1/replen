# Replenishment Dashboard — System Design & Infrastructure

> **Version:** 1.1  
> **Last Updated:** 2026-08-03  
> **Stack:** FastAPI · Alpine.js · BigQuery · Google Kubernetes Engine (GKE)  
> **Note:** No Redis. Job state is stored in BigQuery (`pipeline_run_log`).

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Infrastructure Architecture](#2-infrastructure-architecture)
3. [Component Breakdown](#3-component-breakdown)
4. [Data Flow](#4-data-flow)
5. [API Design](#5-api-design)
6. [Scheduler & Worker Design](#6-scheduler--worker-design)
7. [Project Structure](#7-project-structure)
8. [Local Development Setup](#8-local-development-setup)
9. [Dockerfile](#9-dockerfile)
10. [Kubernetes Manifests Overview](#10-kubernetes-manifests-overview)
11. [Environment Variables](#11-environment-variables)

---

## 1. System Overview

The Replenishment Dashboard is a web application that:

- **Visualizes** replenishment order data computed by the pipeline (`replen.py`)
- **Triggers** the replenishment pipeline manually via a dashboard button
- **Schedules** the pipeline automatically every day at **06:00 WIB**
- **Reads** all data from **BigQuery** mart tables
- **Writes** pipeline output + run logs back to BigQuery

### Why no Redis?

The pipeline takes 1–3 minutes. The browser polls a status endpoint every 3 seconds.
BigQuery returns a simple `SELECT status FROM pipeline_run_log LIMIT 1` in ~200–400ms.
That's well within any acceptable threshold and introduces **zero extra infrastructure**.

The dashboard never freezes or hangs because the pipeline runs in a **FastAPI BackgroundTask** —
the web server stays fully responsive to all other requests throughout the entire run.

```
Browser            FastAPI (web server)         BigQuery
  │                       │                        │
  ├─ POST /run ──────────►│                        │
  │◄─ 202 immediately ────┤  (does not block)      │
  │                       │                        │
  │  (UI stays live)      │  [BackgroundTask]      │
  │                       │──── pipeline runs ────►│
  │                       │                        │
  ├─ GET /status ────────►│──── SELECT status ────►│  ~200ms
  │◄─ { "running" } ──────│◄───────────────────────│
  ├─ GET /status ────────►│──── SELECT status ────►│  ~200ms
  │◄─ { "success" } ──────│◄───────────────────────│
  │                       │                        │
  ├─ GET /dashboard ─────►│──── reads mart ────────►│
  │◄─ fresh data ─────────│◄───────────────────────│
```

---

## 2. Infrastructure Architecture

### Local Development

```
Developer Machine
│
├── docker-compose up --build
│     └── [web]  FastAPI app  →  localhost:8000
│                (no extra services needed)
│
└── GCP credentials via Application Default Credentials (ADC)
      └── gcloud auth application-default login
            └── BigQuery (cloud datasets, read + write)
```

### Production — Google Cloud (GKE)

```
Google Cloud Platform
│
├── GKE Cluster: replenishment-cluster
│     │
│     ├── Namespace: replenishment
│     │     │
│     │     ├── Deployment: replenishment-web
│     │     │     └── Pod × 2: fastapi-app
│     │     │           Serves API + static HTML/JS/CSS
│     │     │           Image: gcr.io/{PROJECT}/replenishment-web:latest
│     │     │
│     │     ├── CronJob: replenishment-scheduler
│     │     │     Schedule: "0 23 * * *"  (23:00 UTC = 06:00 WIB)
│     │     │     concurrencyPolicy: Forbid
│     │     │     Image: same image, command: python run_pipeline.py
│     │     │
│     │     ├── Service: replenishment-web-svc (ClusterIP + Ingress)
│     │     │
│     │     └── ConfigMap + Secret: env vars, project config
│     │
│     └── Workload Identity
│           GKE Service Account → GCP Service Account
│           Grants BigQuery Data Editor + Job User roles
│           No key files needed anywhere
│
├── Artifact Registry
│     gcr.io/{PROJECT}/replenishment-web
│
└── BigQuery
      ├── Dataset: dw_replenishment_dev   (source tables)
      └── Dataset: dw_replenishment_prod  (mart tables + control tables + run log)
```

---

## 3. Component Breakdown

### 3.1 FastAPI Application

| Responsibility | Details |
|---|---|
| Serve UI | Mounts `static/` → serves `index.html` at `/` |
| REST API | All endpoints at `/api/v1/` |
| Manual trigger | `POST /api/v1/replenishment/run` → dispatches pipeline as BackgroundTask |
| Status check | `GET /api/v1/replenishment/status` → queries `pipeline_run_log` in BQ |
| Dashboard data | `GET /api/v1/dashboard/*` → queries mart tables in BQ |
| Health check | `GET /healthz` → K8s liveness/readiness probe |

### 3.2 Background Task (FastAPI BackgroundTasks)

| Responsibility | Details |
|---|---|
| Execute pipeline | Runs all `replen.py` logic as Python modules (not subprocess) |
| Concurrency guard | Before starting: query BQ for status. If `'running'` → return 409 |
| Write job state | `INSERT` to `pipeline_run_log` at start. `UPDATE` on finish/fail |
| Write output | Write `mart_replenishment_order` + `mart_abc_xyz_classification` to BQ |

No separate worker process. No message queue. One container handles everything.

### 3.3 Scheduler

| Environment | Mechanism |
|---|---|
| **Local** | `APScheduler` (AsyncIOScheduler) embedded in FastAPI on startup |
| **Production** | Kubernetes `CronJob` with `concurrencyPolicy: Forbid` |

### 3.4 Alpine.js Frontend

| Responsibility | Details |
|---|---|
| Served by | FastAPI `StaticFiles` mount — no Nginx, no Node.js, no build step |
| Data fetching | `fetch()` to `/api/v1/*` endpoints |
| Status polling | `setInterval` every 3s during a run, stops on `success`/`failed` |
| Reactivity | Alpine.js `x-data`, `x-bind`, `x-for`, `x-show` |

### 3.5 Job State — `pipeline_run_log` (BigQuery table)

Replaces Redis entirely. Lives in `dw_replenishment_prod`.

```sql
CREATE TABLE dw_replenishment_prod.pipeline_run_log (
  run_id          STRING    NOT NULL,
  cycle_id        STRING,
  status          STRING    NOT NULL,  -- 'running' | 'success' | 'failed'
  triggered_by    STRING    NOT NULL,  -- 'manual' | 'scheduler'
  started_at      TIMESTAMP NOT NULL,
  completed_at    TIMESTAMP,
  duration_sec    FLOAT64,
  error_message   STRING
);
```

**Status query (used by polling endpoint):**
```sql
SELECT run_id, cycle_id, status, triggered_by, started_at, completed_at
FROM dw_replenishment_prod.pipeline_run_log
ORDER BY started_at DESC
LIMIT 1
```

**Duplicate run guard (used before starting a new run):**
```sql
SELECT COUNT(*) as active_runs
FROM dw_replenishment_prod.pipeline_run_log
WHERE status = 'running'
  AND started_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 HOUR)
```

---

## 4. Data Flow

### 4.1 Manual Run (Button Click)

```
Browser (Alpine.js)
  │
  │  POST /api/v1/replenishment/run
  ▼
FastAPI /run endpoint
  │
  ├─ Query BQ: any active runs in last 2h?
  │     YES → return HTTP 409 "Pipeline already running"
  │     NO  → continue
  │
  ├─ INSERT pipeline_run_log (run_id, status='running', triggered_by='manual')
  ├─ Generate cycle_id (UUID)
  ├─ Dispatch pipeline to FastAPI BackgroundTasks (non-blocking)
  │
  └─ Return HTTP 202 { "run_id": "...", "cycle_id": "...", "status": "running" }

        ┌─────────────────────────────────────────────────┐
        │  BackgroundTask: pipeline_runner(cycle_id)      │
        │                                                 │
        │  [1] Read from BQ (parallel ThreadPoolExecutor) │
        │        fact_weekly_sales                        │
        │        fact_stock_position                      │
        │        dim_product_price                        │
        │        dim_unit_conversion                      │
        │        stg_tmp_warehouse_stock                  │
        │        ref_planogram                            │
        │        ctrl_hold_stockout_rule                  │
        │        ctrl_hold_to_planogram_rule              │
        │        ctrl_replenishment_config                │
        │        ctrl_additional_order                    │
        │        dim_active_pharmacy                      │
        │                                                 │
        │  [2] Run pipeline stages:                       │
        │        ABC Analysis                             │
        │        ABC-XYZ Volatility Analysis              │
        │        Demand Forecasting (SES / Croston)       │
        │        Inventory Targets (min/max stock)        │
        │        Inventory Balancing (stock vs targets)   │
        │        Ordering & Unit Conversion               │
        │        Apply hold rules, planogram overrides,   │
        │        TMP allocation, additional orders        │
        │                                                 │
        │  [3] Write to BQ:                              │
        │        mart_replenishment_order (INSERT)        │
        │        mart_abc_xyz_classification (INSERT)     │
        │                                                 │
        │  [4] UPDATE pipeline_run_log                   │
        │        status='success', completed_at=NOW()     │
        │        (or status='failed', error_message=...)  │
        └─────────────────────────────────────────────────┘

Browser polls GET /api/v1/replenishment/status every 3 seconds
  └─ On success → reload dashboard data automatically
```

### 4.2 Scheduled Run (06:00 WIB Daily)

```
[Local]  APScheduler fires → calls same pipeline_runner() function
[Prod]   K8s CronJob spawns pod → runs run_pipeline.py
                │
                │  Same flow as manual:
                │  INSERT run_log (triggered_by='scheduler')
                │  → pipeline → write mart → UPDATE run_log
                │
                └─ concurrencyPolicy: Forbid (K8s) prevents overlapping
```

### 4.3 Dashboard Data Read

```
Browser loads page
  │
  ├─ GET /api/v1/dashboard/summary
  │     └─ BQ: SELECT ... FROM mart_replenishment_order
  │              WHERE cycle_id = (SELECT MAX(cycle_id)...) → KPI cards
  │
  ├─ GET /api/v1/replenishment/latest
  │     └─ BQ: paginated order rows from latest cycle → table
  │
  └─ GET /api/v1/replenishment/status
        └─ BQ: SELECT status FROM pipeline_run_log LIMIT 1
             → shows last run time, status badge
```

---

## 5. API Design

### Base URL
- **Local:** `http://localhost:8000/api/v1`
- **Production:** `https://replenishment.internal/api/v1`

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Health check (K8s liveness + readiness probe) |
| `GET` | `/api/v1/replenishment/status` | Current/last pipeline run status |
| `POST` | `/api/v1/replenishment/run` | Trigger manual replenishment run |
| `GET` | `/api/v1/replenishment/runs` | History of all run cycles |
| `GET` | `/api/v1/replenishment/latest` | Order rows from the most recent cycle |
| `GET` | `/api/v1/replenishment/{cycle_id}` | Order rows for a specific cycle |
| `GET` | `/api/v1/dashboard/summary` | KPI cards (total value, SKU count, status breakdown) |
| `GET` | `/api/v1/orders` | Paginated + filtered order rows |
| `GET` | `/api/v1/pharmacies` | List active pharmacies |
| `GET` | `/api/v1/products` | List products |

### Query Parameters (`/api/v1/orders`)

| Param | Type | Description |
|---|---|---|
| `cycle_id` | `string` | Filter by specific cycle (default: latest) |
| `pharmacy_id` | `string` | Filter by pharmacy |
| `abc_xyz_class` | `string` | E.g. `AX`, `BZ` |
| `final_status` | `string` | E.g. `Overstock`, `Hold` |
| `page` | `int` | Page number (default: 1) |
| `page_size` | `int` | Rows per page (default: 50, max: 500) |

### Key Response Schemas

```json
// POST /api/v1/replenishment/run  →  202
{
  "run_id": "a1b2c3d4-...",
  "cycle_id": "x9y8z7...",
  "status": "running",
  "triggered_at": "2026-08-03T06:00:00+07:00",
  "triggered_by": "manual"
}

// GET /api/v1/replenishment/status
{
  "run_id": "a1b2c3d4-...",
  "status": "running" | "success" | "failed" | "idle",
  "cycle_id": "x9y8z7...",
  "triggered_by": "manual" | "scheduler",
  "started_at": "2026-08-03T06:00:00+07:00",
  "completed_at": null,
  "duration_sec": null
}

// GET /api/v1/dashboard/summary
{
  "cycle_id": "x9y8z7...",
  "computed_at": "2026-08-03T06:04:22+07:00",
  "total_order_value": 1234567890,
  "total_ordered_skus": 482,
  "total_pharmacies": 24,
  "abc_xyz_breakdown": {
    "AX": 42, "AY": 31, "BX": 27, "BZ": 18
  },
  "status_breakdown": {
    "order_qty": 312,
    "overstock": 58,
    "no_need_to_replenish": 91,
    "hold": 12,
    "not_active": 9
  }
}
```

---

## 6. Scheduler & Worker Design

### Local (APScheduler embedded in FastAPI)

```python
# app/scheduler.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

scheduler = AsyncIOScheduler()

scheduler.add_job(
    func=run_pipeline_job,       # same function as manual trigger
    trigger=CronTrigger(
        hour=6, minute=0,
        timezone=pytz.timezone("Asia/Jakarta")
    ),
    id="daily_replenishment",
    replace_existing=True,
    misfire_grace_time=300,      # if FastAPI was down at 6AM, run within 5min
)
```

### Production (Kubernetes CronJob)

```
Schedule: "0 23 * * *"   (23:00 UTC = 06:00 WIB)
```

The CronJob pod runs `python run_pipeline.py` which calls the same pipeline function.
`concurrencyPolicy: Forbid` means K8s never starts a second pod if the previous is still running.

### Job State Machine

```
  ┌────────┐   trigger    ┌─────────┐
  │  idle  │ ────────────►│ running │
  └────────┘              └────┬────┘
       ▲                       │
       │            ┌──────────┼──────────┐
       │            ▼                     ▼
       │       ┌─────────┐          ┌────────┐
       └───────│ success │          │ failed │
               └─────────┘          └────────┘
                    │                    │
                    └────── reset ───────┘
                         (next trigger)
```

---

## 7. Project Structure

```
replenishment-dashboard/
│
├── app/
│   ├── main.py                   # FastAPI app, router registration, scheduler init
│   ├── scheduler.py              # APScheduler setup (local dev only)
│   ├── dependencies.py           # BigQuery client factory (dependency injection)
│   │
│   ├── api/
│   │   └── v1/
│   │       ├── replenishment.py  # /replenishment/* endpoints
│   │       ├── dashboard.py      # /dashboard/summary endpoint
│   │       ├── orders.py         # /orders paginated endpoint
│   │       ├── pharmacies.py
│   │       └── products.py
│   │
│   ├── pipeline/
│   │   ├── runner.py             # Entry point: reads BQ, runs stages, writes BQ
│   │   ├── abc_analysis.py       # Refactored from replen.py
│   │   ├── forecasting.py
│   │   ├── inventory_targets.py
│   │   ├── balancing.py
│   │   └── ordering.py
│   │
│   ├── bigquery/
│   │   ├── client.py             # BigQuery client singleton
│   │   ├── queries.py            # All SQL strings (no raw SQL in route handlers)
│   │   └── writer.py             # Write mart tables + pipeline_run_log
│   │
│   └── schemas/
│       ├── replenishment.py      # Pydantic models for API responses
│       └── dashboard.py
│
├── static/
│   ├── index.html                # Single-page dashboard
│   ├── css/style.css
│   └── js/app.js                 # Alpine.js components
│
├── run_pipeline.py               # Standalone script (K8s CronJob entrypoint)
│
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── requirements.txt
│
└── k8s/
    ├── namespace.yaml
    ├── deployment-web.yaml
    ├── cronjob.yaml
    ├── service.yaml
    ├── ingress.yaml
    └── configmap.yaml
```

---

## 8. Local Development Setup

### Prerequisites

- Docker Desktop
- Python 3.11+
- `gcloud` CLI: `gcloud auth application-default login`
- BigQuery access on your GCP project

### Steps

```bash
# 1. Clone repo and configure
git clone <repo-url>
cd replenishment-dashboard
cp .env.example .env
# Fill in GCP_PROJECT_ID and dataset names in .env

# 2. Start (single service — just the FastAPI app)
docker-compose up --build

# 3. Open dashboard
open http://localhost:8000

# 4. Swagger docs
open http://localhost:8000/docs
```

### Test manual trigger

```bash
# Fire the pipeline
curl -X POST http://localhost:8000/api/v1/replenishment/run

# Poll status (takes 1-3 min to reach 'success')
curl http://localhost:8000/api/v1/replenishment/status

# View results
curl http://localhost:8000/api/v1/dashboard/summary
```

---

## 9. Dockerfile

```dockerfile
# ── Stage 1: Builder ─────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime ─────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy installed packages from builder (keeps runtime image slim)
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ ./app/
COPY static/ ./static/
COPY run_pipeline.py .

# Non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

EXPOSE 8000

# Default: run web server
# K8s CronJob overrides this with: ["python", "run_pipeline.py"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
```

### `docker-compose.yml` (Local — single service)

```yaml
version: "3.9"

services:
  web:
    build: .
    ports:
      - "8000:8000"
    environment:
      - GCP_PROJECT_ID=${GCP_PROJECT_ID}
      - BQ_DATASET_DEV=${BQ_DATASET_DEV}
      - BQ_DATASET_PROD=${BQ_DATASET_PROD}
      - ENV=local
    volumes:
      # Mount ADC credentials (gcloud auth application-default login)
      - ${APPDATA}/gcloud:/home/appuser/.config/gcloud:ro   # Windows
      # - ${HOME}/.config/gcloud:/home/appuser/.config/gcloud:ro  # Linux/Mac
      # Hot reload in dev
      - ./app:/app/app
      - ./static:/app/static
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

> **Note (Windows):** ADC credentials on Windows live in `%APPDATA%\gcloud`.
> The `${APPDATA}` variable in docker-compose resolves this correctly.

---

## 10. Kubernetes Manifests Overview

### `deployment-web.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: replenishment-web
  namespace: replenishment
spec:
  replicas: 2
  selector:
    matchLabels:
      app: replenishment-web
  template:
    spec:
      serviceAccountName: replenishment-ksa    # Workload Identity
      containers:
        - name: web
          image: gcr.io/{PROJECT_ID}/replenishment-web:latest
          ports:
            - containerPort: 8000
          envFrom:
            - configMapRef:
                name: replenishment-config
          livenessProbe:
            httpGet: { path: /healthz, port: 8000 }
            initialDelaySeconds: 10
            periodSeconds: 15
          readinessProbe:
            httpGet: { path: /healthz, port: 8000 }
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests: { cpu: "250m", memory: "512Mi" }
            limits:   { cpu: "1000m", memory: "2Gi" }
```

### `cronjob.yaml`

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: replenishment-scheduler
  namespace: replenishment
spec:
  schedule: "0 23 * * *"         # 23:00 UTC = 06:00 WIB
  timeZone: "Asia/Jakarta"       # K8s 1.27+ feature
  concurrencyPolicy: Forbid      # never overlap two runs
  successfulJobsHistoryLimit: 7  # keep last 7 days of job history
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: replenishment-ksa
          restartPolicy: OnFailure
          containers:
            - name: pipeline-runner
              image: gcr.io/{PROJECT_ID}/replenishment-web:latest
              command: ["python", "run_pipeline.py"]
              envFrom:
                - configMapRef:
                    name: replenishment-config
              resources:
                requests: { cpu: "500m",  memory: "1Gi" }
                limits:   { cpu: "2000m", memory: "4Gi" }
```

### `configmap.yaml`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: replenishment-config
  namespace: replenishment
data:
  GCP_PROJECT_ID: "mclinica-analytics"
  BQ_DATASET_DEV: "dw_replenishment_dev"
  BQ_DATASET_PROD: "dw_replenishment_prod"
  ENV: "production"
  TZ: "Asia/Jakarta"
```

---

## 11. Environment Variables

### `.env.example`

```env
# ── GCP ─────────────────────────────────────
GCP_PROJECT_ID=mclinica-analytics
BQ_DATASET_DEV=dw_replenishment_dev
BQ_DATASET_PROD=dw_replenishment_prod

# ── App ─────────────────────────────────────
ENV=local                   # local | production
TZ=Asia/Jakarta

# ── GCP Auth ────────────────────────────────
# Production → Workload Identity (no key file needed)
# Local      → run: gcloud auth application-default login
#              Credentials auto-discovered from ~/.config/gcloud
```

---

## Architecture Decision Notes

| Decision | Choice | Reason |
|---|---|---|
| **Job state** | BigQuery `pipeline_run_log` table | Already have BQ, zero extra infra. 200–400ms query is fine for 3s polling interval |
| **No Redis** | Removed | Overkill for a 1–3 min job polled every 3s. Adds infra complexity for no real benefit |
| **Background tasks** | FastAPI `BackgroundTasks` | Pipeline is one long task, not a queue. Simple, no Celery overhead |
| **Frontend** | Alpine.js served as static files | No build step, no Node.js server, zero extra complexity |
| **Scheduler (local)** | APScheduler embedded in FastAPI | No extra process in docker-compose |
| **Scheduler (prod)** | K8s CronJob | Reliable, restartable, DE-friendly, `concurrencyPolicy: Forbid` built in |
| **Auth** | Workload Identity (prod) / ADC (local) | No service account key files to store or rotate |
| **Replicas** | 2 for web pod | Basic HA. Duplicate run guard handled by BQ status check, not distributed lock |
| **Image strategy** | Multi-stage Dockerfile | Slim runtime image; same image reused for web + CronJob with different `command:` |
