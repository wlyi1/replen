# Business Requirements Document (BRD)
# Inofarma Replenishment System

> **Document Type:** Business Requirements Document (BRD)  
> **Version:** 1.0  
> **Date:** 2026-08-11  
> **Prepared by:** Infrastructure & Supply Chain Team  
> **Status:** Draft  
> **Audience:** Business Stakeholders, Product Management, Procurement, Store Operations

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Business Problem Statement](#2-business-problem-statement)
3. [Business Objectives & Success Metrics](#3-business-objectives--success-metrics)
4. [Stakeholders & User Personas](#4-stakeholders--user-personas)
5. [Scope](#5-scope)
6. [Business Process Overview](#6-business-process-overview)
7. [Functional Business Requirements](#7-functional-business-requirements)
8. [Non-Functional Requirements](#8-non-functional-requirements)
9. [Business Rules](#9-business-rules)
10. [Assumptions & Constraints](#10-assumptions--constraints)
11. [Glossary](#11-glossary)

---

## 1. Executive Summary

Inofarma operates a network of pharmacies that require daily replenishment of pharmaceutical and retail SKUs sourced from a central TMP warehouse. Previously, replenishment orders were computed manually by a Python script (`replen.py`) and exported as CSV files or Google Sheets, with no historical record of past order cycles and no systematic override capability.

This document defines the business requirements for the **Inofarma Replenishment System** — a web-based, automated replenishment platform that replaces the manual export workflow with a structured data pipeline, persistent historical data storage, and an interactive dashboard for operations teams.

The system computes daily purchase recommendations for every active pharmacy-SKU combination using statistical demand forecasting, ABC-XYZ inventory classification, planogram target rules, and a multi-tier custom override configuration. All results are stored in Google BigQuery for full auditability, and are accessible through a web dashboard that refreshes automatically each day at 06:00 WIB.

---

## 2. Business Problem Statement

### 2.1 Current Situation (Before System)

| Pain Point | Description |
|---|---|
| **No Historical Audit Trail** | Replenishment orders were calculated in-memory and exported to CSV/Google Sheets with no persistence. There was no way to compare today's orders vs yesterday's, or trace why a specific SKU was or was not ordered. |
| **Manual Process & Human Error** | The pipeline was triggered manually by an analyst, requiring human intervention daily. If the analyst was absent or late, pharmacies would miss their replenishment window. |
| **No Real-Time Visibility** | Store managers and procurement had no centralized view of order recommendations. They received flat files with no filtering, searching, or drill-down capability. |
| **No Override Mechanism** | There was no structured way to suppress, hold, or modify replenishment for specific SKUs or stores. Analysts had to manually edit the CSV output. |
| **No TMP Warehouse Awareness** | Orders were computed without considering available stock at the central warehouse. Over-ordering against low warehouse stock was a frequent operational issue. |
| **No Classification Tracking** | ABC-XYZ product classifications were not persisted, making it impossible to track whether product velocity or revenue contribution had changed over time. |

### 2.2 Impact on Business

- **Stockouts** in high-demand SKUs due to delayed or missed runs.
- **Overstock** in slow-moving SKUs due to no automatic classification and mode control (`min` vs `max`).
- **Working capital inefficiency** from unoptimized order quantities.
- **Poor cross-team coordination** between procurement, warehouse, and store operations due to lack of a shared real-time view.

---

## 3. Business Objectives & Success Metrics

### 3.1 Primary Business Objectives

| # | Objective | Priority |
|---|---|---|
| O1 | Automate daily replenishment order calculation without analyst intervention | Critical |
| O2 | Persist full historical replenishment records for audit and trend analysis | Critical |
| O3 | Provide a real-time web dashboard for procurement and operations teams | Critical |
| O4 | Enable structured override rules at class-level and SKU/store-level granularity | High |
| O5 | Integrate central TMP warehouse stock availability into order calculations | High |
| O6 | Support manual pipeline re-runs on demand (e.g., emergency runs outside 06:00 WIB) | Medium |
| O7 | Provide historical cycle comparison for analytics and procurement planning | Medium |

### 3.2 Success Metrics (KPIs)

| Metric | Target |
|---|---|
| **Pipeline Uptime** | Daily automated run completes successfully >= 99% of business days |
| **Pipeline Execution Time** | Full run (all pharmacies x all SKUs) completes in <= 3 minutes |
| **Dashboard Availability** | Web dashboard accessible and loading data within <= 5 seconds |
| **Data Freshness** | Dashboard reflects latest run cycle within 30 seconds of completion |
| **Override Accuracy** | 100% of override rules applied without silent failures (flag visible in output) |
| **Historical Retention** | Minimum 2 years of full replenishment cycle history retained in BigQuery |

---

## 4. Stakeholders & User Personas

### 4.1 Stakeholder Map

| Role | Type | Interest |
|---|---|---|
| **Head of Supply Chain** | Primary Business Sponsor | Overall ordering performance, working capital, stockout reduction |
| **Procurement Team** | Primary End User | Daily review of order recommendations, override management |
| **Store / Pharmacy Manager** | Secondary End User | Store-level order visibility |
| **Warehouse Operations (TMP)** | Downstream Consumer | Receiving finalized order quantities, stock allocation |
| **Data Engineering / Infrastructure** | System Owner | Pipeline reliability, data integrity, schema governance |
| **Finance** | Reporting Stakeholder | Total order value per cycle, trend analysis |

### 4.2 User Personas

---

#### Persona 1 - Procurement Analyst (Primary Dashboard User)

> *"I need to see today's order recommendations for all pharmacies, filter by store or SKU category, and quickly identify any holds, overstock, or anomalies — without downloading a spreadsheet."*

**Goals:**
- Review total daily order value and SKU count at a glance
- Filter by pharmacy, ABC-XYZ class, or order status
- Understand why a specific SKU was flagged as `Hold`, `Overstock`, or `No Need to Replenish`
- Manage override rules (hold rules, hold-to-planogram, additional orders) directly from the UI

**Frustrations (Before System):**
- Had to wait for analyst to run the script and share a CSV
- No way to filter or search within a flat CSV file
- No visibility into overrides — had to manually edit output files

---

#### Persona 2 - Store / Pharmacy Manager

> *"I want to know what's being ordered for my store today and whether anything critical is being held or understocked."*

**Goals:**
- View SKU-level order detail for their specific store
- Identify items on `Hold` or `Hold to Stockout` that may need attention
- See current stock position alongside order recommendations

**Frustrations (Before System):**
- No real-time visibility; had to request the CSV from procurement
- Could not distinguish between algorithm-driven holds vs. manual overrides

---

#### Persona 3 - Supply Chain Manager (Head of Supply Chain)

> *"I want to see the macro picture — total order value trend over cycles, ABC classification breakdown, and whether our replenishment coverage is improving."*

**Goals:**
- KPI summary cards updated daily (total order value, SKU coverage, pharmacies covered)
- Trend visibility across past cycles
- Confidence that the pipeline runs reliably without manual intervention

---

## 5. Scope

### 5.1 In-Scope

| # | Scope Item |
|---|---|
| S1 | Automated daily replenishment pipeline running at **06:00 WIB** every business day |
| S2 | Manual on-demand pipeline trigger via dashboard button |
| S3 | ABC revenue-tier classification per pharmacy (A through E) |
| S4 | XYZ demand volatility classification (X = stable, Y = moderate, Z = highly variable) |
| S5 | Demand forecasting using **SES (Simple Exponential Smoothing)** and **Croston's Method** for intermittent demand |
| S6 | Statistical safety stock calculation using Z-score, standard deviation, and configurable lead times |
| S7 | Min/Max stock target computation per pharmacy per SKU |
| S8 | Inventory balance check (current effective stock vs. targets) to determine order quantity |
| S9 | Unit conversion from sales unit (e.g., strip) to ordering unit (e.g., box) |
| S10 | TMP central warehouse stock availability check and proportional allocation across pharmacies |
| S11 | Planogram (POG) target quantity override logic for qualifying SKUs |
| S12 | Custom hold rules: `Hold` and `Hold to Stockout` per SKU, optionally scoped to specific stores |
| S13 | Custom replenishment config overrides: per ABC-XYZ class or per SKU, optionally scoped to specific stores, with priority precedence |
| S14 | Additional/supplementary order injection per SKU per pharmacy |
| S15 | Persistent historical storage of all replenishment cycles in BigQuery (append per run) |
| S16 | Web dashboard for real-time order visibility, status monitoring, and configuration management |
| S17 | Full run audit trail (run ID, cycle ID, triggered by, duration, error messages) |

### 5.2 Out-of-Scope

| # | Out-of-Scope Item |
|---|---|
| OS1 | Automatic transmission of purchase orders to external suppliers or ERP systems |
| OS2 | Store-level POS (Point of Sale) transaction processing or checkout logic |
| OS3 | Supplier price negotiation or procurement contract management |
| OS4 | Physical inventory counting or warehouse management operations |
| OS5 | Cross-network stock transfers between pharmacies (lateral rebalancing) |
| OS6 | Demand forecasting beyond 1-week lookahead horizon |
| OS7 | Multi-currency or multi-tax zone financial calculations |

---

## 6. Business Process Overview

### 6.1 Before (Current State - As-Is)

```
[06:00 WIB]
Analyst manually runs replen.py
        |
        |-- Reads source Google Sheets + SQL exports
        |-- Computes orders in-memory (Python DataFrames)
        |-- Manually edits CSV for holds / overrides
        +-- Exports CSV / uploads to Google Sheets
                |
                |-- Shares link with procurement team via chat
                +-- No persistence, no audit, no real-time access
```

**Problems:** Analyst dependency, manual overrides, zero history, no visibility.

---

### 6.2 After (Target State - To-Be)

```
[06:00 WIB -- Automated, No Human Needed]
Kubernetes CronJob triggers pipeline
        |
        |-- Reads from BigQuery (sales history, stock, prices,
        |   conversion factors, planogram data, override rules)
        |
        |-- Computes for every active Pharmacy x SKU:
        |       ABC-XYZ Classification
        |       Demand Forecast (SES / Croston)
        |       Safety Stock & Min/Max Targets
        |       Order Quantity (with all overrides applied)
        |       TMP Warehouse Allocation
        |
        |-- Writes results to BigQuery (historical append)
        |
        +-- Dashboard auto-refreshes for all users

[Procurement Team -- Real-Time Access]
        |
        |-- Views KPI cards (total value, SKUs ordered, status breakdown)
        |-- Filters by pharmacy, ABC-XYZ class, status
        |-- Manages override rules (holds, configs, additional orders)
        +-- Exports or reviews specific historical cycles
```

---

## 7. Functional Business Requirements

### FBR-01 — Automated Daily Replenishment Run

| Field | Detail |
|---|---|
| **ID** | FBR-01 |
| **Title** | Automated Daily Replenishment Pipeline |
| **Priority** | Critical |
| **Description** | The system SHALL automatically execute the full replenishment pipeline every day at **06:00 WIB** without any manual intervention. If a run is already in progress, the system SHALL prevent a duplicate run and log a concurrent-run conflict. |
| **Acceptance Criteria** | Pipeline triggers at 06:00 WIB daily without manual action. Duplicate runs within a 2-hour window are blocked. A completion status (success or failed) is recorded in the audit log. |

---

### FBR-02 — Manual On-Demand Pipeline Trigger

| Field | Detail |
|---|---|
| **ID** | FBR-02 |
| **Title** | Manual Run Trigger via Dashboard |
| **Priority** | Critical |
| **Description** | Authorized users SHALL be able to trigger a replenishment run at any time via a button on the web dashboard. The system SHALL return an immediate acknowledgement and run the pipeline in the background. |
| **Acceptance Criteria** | Dashboard shows a "Run Pipeline" button. Clicking triggers an immediate 202 Accepted response (non-blocking). Dashboard polls for status and auto-refreshes data on completion. |

---

### FBR-03 — ABC-XYZ Product Classification

| Field | Detail |
|---|---|
| **ID** | FBR-03 |
| **Title** | Automated ABC-XYZ Inventory Classification |
| **Priority** | Critical |
| **Description** | The system SHALL classify every active pharmacy-SKU pair into a combined **ABC-XYZ class** per replenishment cycle. **ABC** ranks SKUs by cumulative revenue contribution (A = top 70%, B = 70-90%, C = 90-99%, D = 99-99.9%, E = bottom 0.1%). **XYZ** ranks SKUs by demand variability using the Coefficient of Variation (CV): X = low variability (CV < 0.5), Y = moderate (0.5-1.0), Z = high (CV > 1.0). |
| **Acceptance Criteria** | Every output row has an `abc_xyz_class` (e.g., `AX`, `BZ`). Classification results are persisted per cycle in `mart_abc_xyz_classification`. |

---

### FBR-04 — Demand Forecasting

| Field | Detail |
|---|---|
| **ID** | FBR-04 |
| **Title** | Statistical Weekly Demand Forecasting |
| **Priority** | Critical |
| **Description** | The system SHALL forecast next-week demand for each pharmacy-SKU using the appropriate statistical model based on demand pattern. For regular demand SKUs, use **Simple Exponential Smoothing (SES)**. For intermittent/sporadic demand SKUs (many zero-sales weeks), use **Croston's Method**. If no historical sales data is available, forecast defaults to zero (`'None'` method). |
| **Acceptance Criteria** | Output row contains `forecast_weekly_qty` and `forecast_method` (SES / Croston / None). |

---

### FBR-05 — Inventory Target Calculation (Min / Max Stock)

| Field | Detail |
|---|---|
| **ID** | FBR-05 |
| **Title** | Safety Stock and Min/Max Target Computation |
| **Priority** | Critical |
| **Description** | The system SHALL compute three inventory target levels per pharmacy-SKU. **Buffer Stock (Safety Stock):** `CEIL(Z x sigma x sqrt(LT))` where Z = service level Z-score from ABC-XYZ class, sigma = weekly demand standard deviation, LT = lead time in weeks. **Minimum Stock (Reorder Point):** `CEIL(forecast_weekly_qty + buffer_stock_qty)`. **Maximum Stock (Order-Up-To Level):** `CEIL(minimum_stock_qty + forecast_weekly_qty)`. |
| **Acceptance Criteria** | Output rows contain `buffer_stock_qty`, `minimum_stock_qty`, `maximum_stock_qty`. Lead time and mode are configurable via `ctrl_replenishment_config`. |

---

### FBR-06 — Replenishment Quantity Determination

| Field | Detail |
|---|---|
| **ID** | FBR-06 |
| **Title** | Order Quantity Calculation with Mode Logic |
| **Priority** | Critical |
| **Description** | Based on current effective stock vs. inventory targets, the system SHALL determine a raw replenishment quantity using the configured mode. **`max` mode:** Order up to `maximum_stock_qty`. **`min` mode:** Order up to `minimum_stock_qty`. The output SHALL also classify the outcome as a status label: `Order Qty`, `Overstock`, `No Need to Replenish`, `Hold`, `Hold to Stockout`, `No Follow`, or `Manually Review`. |
| **Acceptance Criteria** | Every output row has a `replenishment_qty_raw`, `replenishment_status`, and `replenishment_mode`. |

---

### FBR-07 — Unit Conversion (Sales Unit to Ordering Unit)

| Field | Detail |
|---|---|
| **ID** | FBR-07 |
| **Title** | Sales-to-Ordering Unit Conversion |
| **Priority** | Critical |
| **Description** | Because pharmacies track stock in **sales units** (e.g., strips) but order in **ordering units** (e.g., boxes), the system SHALL convert the raw replenishment quantity: `ordered_qty_converted = CEIL(replenishment_qty_raw / conversion_factor)`. Conversion factors are managed in `dim_unit_conversion`. If no conversion factor exists, the system defaults to factor = 1. |
| **Acceptance Criteria** | Output rows contain `ordered_qty_converted` and `conversion_factor`. |

---

### FBR-08 — TMP Warehouse Stock Allocation

| Field | Detail |
|---|---|
| **ID** | FBR-08 |
| **Title** | Central Warehouse Stock Availability Check and Allocation |
| **Priority** | High |
| **Description** | Before finalizing order quantities, the system SHALL check available stock at the **TMP central warehouse** per SKU. If total pharmacy demand for a SKU exceeds TMP availability, the system SHALL allocate available stock **proportionally** across all pharmacies ordering that SKU. SKUs with status `'discontinue'` or `'not_active'` at TMP SHALL be flagged accordingly. |
| **Acceptance Criteria** | Output rows contain `tmp_available_qty`, `tmp_item_status`, and `ordered_qty_final` (after allocation). `final_status_after_allocation` reflects whether TMP stock was sufficient or capped. |

---

### FBR-09 — Planogram Override Logic

| Field | Detail |
|---|---|
| **ID** | FBR-09 |
| **Title** | Planogram (POG) Quantity Override |
| **Priority** | High |
| **Description** | For SKUs where the pharmacy is enrolled in the planogram program (`follow_planogram = 'yes'`) and the planogram target gap exceeds the forecast-based order qty, the system SHALL override the order quantity with the raw planogram target gap. A flag `planogram_gap_applied = TRUE` SHALL be set on the output row. |
| **Acceptance Criteria** | `planogram_gap_applied` is set correctly. `status_before_planogram` captures the pre-override status for auditability. |

---

### FBR-10 — Hold & Stockout Suppression Rules

| Field | Detail |
|---|---|
| **ID** | FBR-10 |
| **Title** | SKU Hold Rules — Suppression of Replenishment |
| **Priority** | High |
| **Description** | The system SHALL support two types of hold rules managed by the operations team. **`hold`:** Completely suppress replenishment for this SKU. Order qty forced to 0. **`hold_to_stockout`:** Allow the current stock to sell through naturally; once on-hand falls to 0, further orders are also suppressed. Rules can be applied globally to **all pharmacies** or scoped to a **specific list of pharmacy IDs**. |
| **Acceptance Criteria** | Affected rows show the appropriate `final_status` label. Scope (all vs. specific) is respected correctly. |

---

### FBR-11 — Custom Replenishment Configuration Override

| Field | Detail |
|---|---|
| **ID** | FBR-11 |
| **Title** | Per-Class and Per-SKU/Store Replenishment Config Override |
| **Priority** | High |
| **Description** | The system SHALL allow operations teams to override the default lead time and replenishment mode at two granularity levels, applied using the following **priority precedence (Most Specific Wins)**: 1. SKU + Specific Store (highest priority). 2. SKU + All Stores. 3. ABC-XYZ Class + Specific Store. 4. ABC-XYZ Class + All Stores (default baseline). Only the highest-matching rule is applied per pharmacy-SKU combination. |
| **Acceptance Criteria** | Correct lead time and mode applied per row based on precedence. No double-application of conflicting rules. Override source is visible in output for transparency. |

---

### FBR-12 — Additional / Supplementary Orders

| Field | Detail |
|---|---|
| **ID** | FBR-12 |
| **Title** | External Additional Order Injection |
| **Priority** | Medium |
| **Description** | The system SHALL support injection of manually specified additional order quantities (e.g., promotional stocking, event preparation) per pharmacy per SKU. These quantities SHALL be merged into the final order quantity and flagged with `is_additional_external_order = TRUE`. |
| **Acceptance Criteria** | Additional quantities are reflected in `ordered_qty_final`. `is_additional_external_order` flag is set correctly on affected rows. |

---

### FBR-13 — Historical Replenishment Data Persistence

| Field | Detail |
|---|---|
| **ID** | FBR-13 |
| **Title** | Full Historical Cycle Persistence in BigQuery |
| **Priority** | Critical |
| **Description** | Every replenishment run SHALL produce a unique `cycle_id` (UUID). All computed rows for that run SHALL be **appended** (INSERT) into `mart_replenishment_order` — never overwritten. This enables full historical comparison across cycles and long-term trend analysis. Dashboard defaults to the latest `cycle_id` but supports querying any historical cycle. |
| **Acceptance Criteria** | Each run has a globally unique `cycle_id`. Old cycles are never deleted or overwritten. Dashboard can filter and display any historical cycle. |

---

### FBR-14 — Web Dashboard

| Field | Detail |
|---|---|
| **ID** | FBR-14 |
| **Title** | Replenishment Dashboard — Visibility and Control |
| **Priority** | Critical |
| **Description** | The system SHALL provide a web-based dashboard accessible to authorized users. The dashboard SHALL include: **KPI Cards:** Total order value, SKU count ordered, pharmacy count, status breakdown. **Order Detail Table:** Paginated, filterable by pharmacy, ABC-XYZ class, and replenishment status. **Pipeline Status:** Live indicator showing last run time, trigger source, and duration. **Configuration Management:** UI for managing hold rules, planogram hold rules, replenishment config overrides, and additional orders. |
| **Acceptance Criteria** | Dashboard loads and displays latest cycle data within 5 seconds. All filters function correctly. Configuration changes are persisted to BigQuery control tables. |

---

### FBR-15 — Run Audit Log

| Field | Detail |
|---|---|
| **ID** | FBR-15 |
| **Title** | Full Pipeline Run Audit Trail |
| **Priority** | Critical |
| **Description** | The system SHALL maintain a persistent audit log of every pipeline execution in `pipeline_run_log` including: run ID, cycle ID, trigger source (`manual` or `scheduler`), start and completion timestamps, total duration in seconds, and any error message on failure. |
| **Acceptance Criteria** | Every run (successful or failed) produces a log entry. Error message is captured on failure for debugging. Log is queryable from the dashboard for operations review. |

---

## 8. Non-Functional Requirements

### NFR-01 — Performance

| Requirement | Target |
|---|---|
| Full pipeline execution (all pharmacies x all SKUs) | <= 3 minutes |
| Dashboard initial page load | <= 5 seconds |
| Dashboard API response time (per endpoint) | <= 2 seconds |
| Status polling latency (during active run) | <= 400 ms per poll |

### NFR-02 — Availability & Reliability

| Requirement | Target |
|---|---|
| Dashboard web service uptime | >= 99.5% (monthly) |
| Scheduled pipeline success rate | >= 99% of business days |
| Concurrent run prevention | 100% — no duplicate runs within a 2-hour window |
| Misfire recovery (if service was down at 06:00 WIB) | Auto-retries within 5 minutes |

### NFR-03 — Scalability

| Requirement | Note |
|---|---|
| Data volume | 15 MB inserted per daily run; approximately 5.5 GB after 1 year — well within BigQuery limits |
| Query performance | BigQuery partition-by-date on `computed_at` ensures daily queries scan only 15 MB regardless of total table size |
| Pipeline computation | ThreadPoolExecutor parallel reads from BigQuery; computations are vectorized pandas operations |

### NFR-04 — Security

| Requirement | Note |
|---|---|
| BigQuery authentication | Workload Identity (GKE Service Account to GCP Service Account). No key files in containers or repositories |
| Data access control | BigQuery IAM roles: `bigquery.dataEditor` and `bigquery.jobUser` scoped to replenishment datasets only |
| Dashboard access | Internal network access only (ClusterIP + Ingress); no public internet exposure |

### NFR-05 — Data Retention & Auditability

| Requirement | Target |
|---|---|
| Historical replenishment cycle retention | Minimum 2 years (730 days) |
| Pipeline run log retention | Indefinite (small table — one row per run) |
| Partition expiration policy | Optional: auto-delete partitions older than 730 days via BigQuery partition expiry |

### NFR-06 — Maintainability

| Requirement | Note |
|---|---|
| Single deployable artifact | One Docker image serves both the web API and the pipeline logic |
| Schema governance | All table schemas defined in `schema.dbml` and `database_schema.md` |
| Configuration management | All business override rules are stored as data in BigQuery `ctrl_` tables — no code changes required to update rules |

---

## 9. Business Rules

| Rule ID | Rule | Applies To |
|---|---|---|
| BR-01 | A pharmacy is included in replenishment ONLY if `is_replenishment_active = TRUE` | `dim_pharmacy` |
| BR-02 | Effective stock = `on_hand_qty + in_transit_qty`. In-transit stock counts against order need. | `fact_stock_position` |
| BR-03 | Sales data window = rolling 7 weeks. Forecast is based on this window only. | `fact_weekly_sales` |
| BR-04 | ABC classification cutoffs: A = top 70%, B = 70-90%, C = 90-99%, D = 99-99.9%, E = remaining. | Classification Engine |
| BR-05 | XYZ cutoffs: X = CV < 0.5, Y = CV 0.5-1.0, Z = CV > 1.0. Unknown = no sales data. | Classification Engine |
| BR-06 | Safety stock formula: `CEIL(Z x sigma x sqrt(LT))`. Z-score is defined per ABC-XYZ class. | Inventory Target Engine |
| BR-07 | Default lead time = 1.0 weeks unless overridden by `ctrl_replenishment_config`. | `ctrl_replenishment_config` |
| BR-08 | Default replenishment mode = `max` unless overridden by `ctrl_replenishment_config`. | `ctrl_replenishment_config` |
| BR-09 | Config override precedence: SKU + Store > SKU + All > Class + Store > Class + All. | `ctrl_replenishment_config` |
| BR-10 | `Hold` rule forces final order qty = 0, regardless of stock levels. | `ctrl_hold_stockout_rule` |
| BR-11 | `Hold to Stockout` suppresses order only when `on_hand_qty = 0`. | `ctrl_hold_stockout_rule` |
| BR-12 | Planogram gap override applies before TMP allocation step. | Planogram Engine |
| BR-13 | TMP allocation: if total demand > TMP available stock, distribute proportionally by computed order qty. | TMP Allocation Engine |
| BR-14 | SKU with TMP status `discontinue` forces final order qty = 0; status labeled `Discontinue`. | TMP Allocation Engine |
| BR-15 | Duplicate run guard: if a run is in `running` status within the last 2 hours, new run is rejected with HTTP 409. | Pipeline Runner |
| BR-16 | Every run generates a unique `cycle_id` (UUID). Results are appended to `mart_replenishment_order`, never overwritten. | Pipeline Runner |

---

## 10. Assumptions & Constraints

### 10.1 Assumptions

| # | Assumption |
|---|---|
| A1 | Source data tables (`fact_weekly_sales`, `fact_stock_position`, `stg_tmp_warehouse_stock`, `ref_planogram`) are refreshed by upstream data pipelines before 06:00 WIB daily. |
| A2 | The TMP warehouse stock snapshot is accurate as of the time the pipeline reads it. Real-time warehouse allocation is not required. |
| A3 | Conversion factors in `dim_unit_conversion` are maintained by the data team and updated when product packaging changes. |
| A4 | A single service level Z-score per ABC-XYZ class is sufficient. Per-pharmacy or per-SKU service levels are not required in the initial release. |
| A5 | Internet-facing public access to the dashboard is not required. The system is accessed only within the internal network. |
| A6 | All monetary values are in a single currency (IDR). Multi-currency is not required. |

### 10.2 Constraints

| # | Constraint |
|---|---|
| C1 | The system must run on Google Cloud Platform (GCP) using BigQuery as the primary data store. No alternative database vendors are permitted. |
| C2 | The pipeline must complete within 3 minutes to ensure results are available by 06:10 WIB at the latest. |
| C3 | No external API calls to ERP or supplier systems — the system is a recommendation engine only. |
| C4 | All override rules must be manageable by non-technical operations staff through the dashboard UI without requiring code deployments. |

---

## 11. Glossary

| Term | Definition |
|---|---|
| **SKU** | Stock Keeping Unit. A unique product identifier (e.g., `Panadol 500mg 10-strip`). |
| **Pharmacy / Store** | An individual Inofarma pharmacy location identified by a `pharmacy_id`. |
| **Replenishment Cycle** | A single complete execution of the pipeline, identified by a unique `cycle_id`. |
| **ABC Classification** | Revenue-based product tier. A = highest revenue contribution, E = lowest. |
| **XYZ Classification** | Demand variability tier. X = most stable, Z = most volatile. |
| **ABC-XYZ Class** | Combined classification (e.g., `AX` = high revenue, stable demand). |
| **SES** | Simple Exponential Smoothing. A time-series forecasting model suitable for regular demand. |
| **Croston's Method** | A forecasting method designed for intermittent (sporadic, zero-heavy) demand patterns. |
| **CV (Coefficient of Variation)** | Standard deviation divided by mean. Measures demand volatility. Higher CV = more variable. |
| **Buffer Stock / Safety Stock** | Extra inventory held to absorb demand uncertainty and lead time variability. |
| **Minimum Stock (Reorder Point)** | Inventory level at which a replenishment order should be placed. |
| **Maximum Stock (Order-Up-To)** | Target inventory level after replenishment. |
| **Lead Time** | Time (in weeks) between placing an order and receiving it at the pharmacy. |
| **TMP Warehouse** | The central Inofarma warehouse that stocks and distributes SKUs to pharmacies. |
| **Planogram (POG)** | A visual merchandising plan that specifies the exact quantity of each product to be displayed on the shelf. |
| **Hold** | A rule that completely suppresses a replenishment order for a specific SKU. |
| **Hold to Stockout** | A rule that suppresses replenishment only after current stock is fully depleted. |
| **Conversion Factor** | Number of sales units per ordering unit (e.g., 10 strips per box = factor 10). |
| **Effective Quantity** | `on_hand_qty + in_transit_qty`. The realistic stock position accounting for incoming orders. |
| **`cycle_id`** | A UUID generated per pipeline run. All output rows from that run share the same `cycle_id`. |
| **`ctrl_` Tables** | BigQuery control tables storing business override rules. Editable via dashboard without code changes. |
| **BRD** | Business Requirements Document. This document. Defines what the system must do from a business perspective. |
| **TDD / SDD** | Technical Design Document / System Design Document. Defines how the system is built. |

---

*End of Document*

---

> **Related Documents:**
> - [SYSTEM_DESIGN.md](./SYSTEM_DESIGN.md) — Technical architecture, infrastructure, API design, and deployment specification
> - [database_schema.md](./database_schema.md) — Full BigQuery table schema definitions
> - [schema.dbml](./schema.dbml) — Machine-readable DBML schema (for ERD generation)
