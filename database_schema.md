# Replenishment System — Database Schema

> **Version:** 1.0  
> **Last Updated:** 2026-08-03  
> **Source:** Derived from `replen.py` pipeline  
> **Datasets:** `dw_replenishment_dev` · `dw_replenishment_prod`

---

## Overview

This document defines the canonical database schema for the Inofarma Replenishment System. The schema is organized into four logical layers following a standard data warehouse pattern:

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 1 – DIMENSION   Master / reference data              │
│  LAYER 2 – FACT        Transactional & positional data      │
│  LAYER 3 – CONTROL     Business rules & configuration       │
│  LAYER 4 – MART        Computed output for dashboard/API    │
└─────────────────────────────────────────────────────────────┘
```

---

## Entity Relationship Diagram

```
dim_pharmacy ──────────────────────────────────────────────────┐
      │                                                        │
      │  pharmacy_id                                           │
      │                                                        │
      ├──── fact_weekly_sales ─────────────────────────────┐   │
      │           │ product_code                           │   │
      │           │                                        │   │
      ├──── fact_stock_position ──── dim_product ──────────┤   │
      │           │                      │                 │   │
      │           │             dim_product_price          │   │
      │           │             dim_unit_conversion        │   │
      │           │                                        │   │
      ├──── ref_planogram                                  │   │
      │                                                    │   │
      │  ctrl_hold_stockout_rule                           │   │
      │  ctrl_hold_to_planogram_rule                       │   │
      │  ctrl_replenishment_config                         │   │
      │  ctrl_additional_order ────────────────────────────┤   │
      │                                                    │   │
      └──── mart_abc_xyz_classification ──────────────────-┘   │
      └──── mart_replenishment_order ──────────────────────────┘
```

---

## Layer 1 – Dimension Tables

### `dim_pharmacy`

> **Replaces:** `phb_aktif`  
> **Dataset:** `dw_replenishment_prod`  
> **Description:** Master list of all pharmacies (active and inactive).

| Column | Type | Nullable | Description |
|---|---|---|---|
| `pharmacy_id` | `STRING` | ❌ | Primary key. Unique pharmacy identifier (e.g. `14001`) |
| `pharmacy_name` | `STRING` | ❌ | Display name of the pharmacy |
| `is_replenishment_active` | `BOOLEAN` | ❌ | Whether this pharmacy is included in the replenishment run. Replaces `replenishment_trigger = 'yes'` |
| `created_at` | `TIMESTAMP` | ❌ | Record creation timestamp |
| `updated_at` | `TIMESTAMP` | ✅ | Last update timestamp |

**Primary Key:** `pharmacy_id`

---

### `dim_product`

> **Replaces:** *(implicit — previously no dedicated product master table)*  
> **Dataset:** `dw_replenishment_prod`  
> **Description:** Master catalog of all SKUs/products.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `product_code` | `STRING` | ❌ | Primary key. Unique product/SKU identifier |
| `product_name` | `STRING` | ✅ | Human-readable product name |
| `created_at` | `TIMESTAMP` | ❌ | Record creation timestamp |
| `updated_at` | `TIMESTAMP` | ✅ | Last update timestamp |

**Primary Key:** `product_code`

---

### `dim_product_price`

> **Replaces:** `pricelist`  
> **Dataset:** `dw_replenishment_dev`  
> **Description:** Per-product unit price used to calculate total order value.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `product_code` | `STRING` | ❌ | FK → `dim_product.product_code` |
| `unit_price` | `NUMERIC` | ❌ | Price per ordering unit (post-conversion) |
| `effective_date` | `DATE` | ✅ | Date from which this price is valid |
| `created_at` | `TIMESTAMP` | ❌ | Record creation timestamp |

**Primary Key:** `product_code, effective_date`  
**Foreign Key:** `product_code` → `dim_product`

---

### `dim_unit_conversion`

> **Replaces:** `conversion_staging`  
> **Dataset:** `dw_replenishment_dev`  
> **Description:** Conversion factor from sales unit (e.g. strip) to ordering unit (e.g. box). Formula: `ordered_qty = CEIL(replenish_qty / conversion_factor)`.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `product_code` | `STRING` | ❌ | FK → `dim_product.product_code` |
| `conversion_factor` | `FLOAT64` | ❌ | Number of sales units per ordering unit (e.g. `10` = 10 strips per box) |
| `created_at` | `TIMESTAMP` | ❌ | Record creation timestamp |

**Primary Key:** `product_code`  
**Foreign Key:** `product_code` → `dim_product`

---

## Layer 2 – Fact & Staging Tables

### `fact_weekly_sales`

> **Replaces:** `mart_windows_7_sales`  
> **Dataset:** `dw_replenishment_dev`  
> **Description:** Weekly sales transactions per pharmacy per SKU. Primary input for ABC-XYZ classification and demand forecasting.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `pharmacy_id` | `STRING` | ❌ | FK → `dim_pharmacy.pharmacy_id` |
| `pharmacy_name` | `STRING` | ✅ | Denormalized pharmacy name for convenience |
| `product_code` | `STRING` | ❌ | FK → `dim_product.product_code` |
| `week_index` | `INTEGER` | ✅ | Relative week number within the rolling window (1 = oldest, N = most recent) |
| `sales_qty` | `FLOAT64` | ❌ | Units sold in this week (sales unit, not ordering unit) |
| `net_revenue` | `FLOAT64` | ❌ | Net sales revenue for this week. Used in ABC revenue ranking |
| `recorded_at` | `TIMESTAMP` | ✅ | Timestamp of the source record |

**Primary Key:** `pharmacy_id, product_code, week_index`  
**Foreign Keys:** `pharmacy_id` → `dim_pharmacy`, `product_code` → `dim_product`

> **Note:** This table is a pre-aggregated mart computed over a rolling 7-week window. Do not use for full historical analysis.

---

### `fact_stock_position`

> **Replaces:** `stok_so_staging_old`  
> **Dataset:** `dw_replenishment_dev`  
> **Description:** Current stock snapshot per pharmacy per SKU, including in-transit stock. `effective_qty = on_hand_qty + in_transit_qty`.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `pharmacy_id` | `STRING` | ❌ | FK → `dim_pharmacy.pharmacy_id` |
| `pharmacy_name` | `STRING` | ✅ | Denormalized pharmacy name |
| `product_code` | `STRING` | ❌ | FK → `dim_product.product_code` |
| `on_hand_qty` | `FLOAT64` | ❌ | Current physical stock quantity (sales unit) |
| `in_transit_qty` | `FLOAT64` | ❌ | Quantity on open purchase orders, not yet received (sales unit) |
| `effective_qty` | `FLOAT64` | ❌ | Computed: `on_hand_qty + in_transit_qty` |
| `follow_planogram` | `STRING` | ✅ | Whether this item follows planogram rules at this pharmacy. Values: `'yes'`, `'no'`, or NULL |
| `snapshot_date` | `DATE` | ✅ | Date of the stock snapshot |

**Primary Key:** `pharmacy_id, product_code`  
**Foreign Keys:** `pharmacy_id` → `dim_pharmacy`, `product_code` → `dim_product`

---

### `stg_tmp_warehouse_stock`

> **Replaces:** `tmp_stock`  
> **Dataset:** `dw_replenishment_dev`  
> **Description:** Available stock at the TMP central warehouse per SKU. Orders are capped to available TMP qty, shared proportionally across all pharmacies ordering that SKU.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `product_code` | `STRING` | ❌ | PK + FK → `dim_product.product_code` |
| `available_qty` | `FLOAT64` | ✅ | Total available stock at TMP warehouse |
| `item_status` | `STRING` | ✅ | Product status at warehouse. Values: `'active'`, `'not_active'`, `'discontinue'` |
| `snapshot_date` | `DATE` | ✅ | Date of the warehouse snapshot |

**Primary Key:** `product_code`  
**Foreign Key:** `product_code` → `dim_product`

---

### `ref_planogram`

> **Replaces:** `gsheet_plano_qty_initial_results`  
> **Dataset:** `dw_replenishment_prod`  
> **Description:** Planogram (POG) target quantities per pharmacy per SKU. Defines the ideal shelf quantity that replenishment should target for planogram items.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `pharmacy_id` | `STRING` | ❌ | FK → `dim_pharmacy.pharmacy_id` |
| `pharmacy_name` | `STRING` | ✅ | Denormalized pharmacy name |
| `product_code` | `STRING` | ❌ | FK → `dim_product.product_code` |
| `planogram_unit_qty` | `FLOAT64` | ❌ | Target shelf quantity in ordering unit (post-conversion) |
| `follow_planogram` | `STRING` | ✅ | Override flag. `'no'` = exclude from planogram logic |
| `item_status` | `STRING` | ✅ | Item lifecycle status: `'active'`, `'not_active'`, `'discontinue'` |
| `updated_at` | `TIMESTAMP` | ✅ | Last sync timestamp from source spreadsheet |

**Primary Key:** `pharmacy_id, product_code`  
**Foreign Keys:** `pharmacy_id` → `dim_pharmacy`, `product_code` → `dim_product`

---

## Layer 3 – Control & Configuration Tables

### `ctrl_hold_stockout_rule`

> **Replaces:** `gsheet_inofarma_control_center_hold_product_stock_out`  
> **Dataset:** `dw_replenishment_prod`  
> **Description:** Rules that suppress replenishment for specific SKUs. `'hold'` blocks completely; `'hold_to_stockout'` allows current stock to deplete before the next replenishment.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `rule_id` | `STRING` | ❌ | Primary key. Auto-generated UUID |
| `product_code` | `STRING` | ❌ | FK → `dim_product.product_code` |
| `rule_type` | `STRING` | ❌ | `'hold'` or `'hold_to_stockout'` |
| `scope_type` | `STRING` | ❌ | `'all'` = all pharmacies; `'specific'` = limited to `applicable_pharmacy_ids` |
| `applicable_pharmacy_ids` | `STRING` | ✅ | Comma-separated pharmacy IDs when `scope_type = 'specific'` |
| `created_at` | `TIMESTAMP` | ❌ | Rule creation timestamp |

**Primary Key:** `rule_id`  
**Foreign Key:** `product_code` → `dim_product`

---

### `ctrl_hold_to_planogram_rule`

> **Replaces:** `gsheet_inofarma_control_center_hold_to_plano`  
> **Dataset:** `dw_replenishment_prod`  
> **Description:** Rules that override the forecast-based order qty with the raw planogram unit qty (without subtracting current stock). Applied before TMP allocation.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `rule_id` | `STRING` | ❌ | Primary key. Auto-generated UUID |
| `product_code` | `STRING` | ❌ | FK → `dim_product.product_code` |
| `rule_type` | `STRING` | ❌ | Rule type label (activation signal — any non-null value activates the override) |
| `scope_type` | `STRING` | ❌ | `'all'` or `'specific'` |
| `applicable_pharmacy_ids` | `STRING` | ✅ | Comma-separated pharmacy IDs when `scope_type = 'specific'` |
| `created_at` | `TIMESTAMP` | ❌ | Rule creation timestamp |

**Primary Key:** `rule_id`  
**Foreign Key:** `product_code` → `dim_product`

---

### `ctrl_replenishment_config`

> **Replaces:** `gsheet_inofarma_control_center_special_control`  
> **Dataset:** `dw_replenishment_prod`  
> **Description:** Override configuration for lead time and replenishment mode per ABC-XYZ category, with optional pharmacy-level scoping.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `config_id` | `STRING` | ❌ | Primary key. Auto-generated UUID |
| `abc_xyz_category` | `STRING` | ❌ | ABC-XYZ class this config applies to. E.g. `'AX'`, `'BZ'` |
| `lead_time_weeks` | `FLOAT64` | ❌ | Lead time in weeks. Used in safety stock formula: `Z × σ × √(LT)`. Default `1.0` |
| `replenishment_mode` | `STRING` | ❌ | `'max'` = order up to maximum stock; `'min'` = order up to minimum stock |
| `scope_type` | `STRING` | ❌ | `'all'` or `'specific'` |
| `applicable_pharmacy_ids` | `STRING` | ✅ | Comma-separated pharmacy IDs when `scope_type = 'specific'` |
| `created_at` | `TIMESTAMP` | ❌ | Config creation timestamp |

**Primary Key:** `config_id`

---

### `ctrl_additional_order`

> **Replaces:** `gsheet_inofarma_control_center_additional_order`  
> **Dataset:** `dw_replenishment_prod`  
> **Description:** Ad-hoc additional quantities added on top of calculated replenishment. Used for promotions, events, or manual business overrides.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `order_id` | `STRING` | ❌ | Primary key. Auto-generated UUID |
| `pharmacy_id` | `STRING` | ❌ | FK → `dim_pharmacy.pharmacy_id` |
| `product_code` | `STRING` | ❌ | FK → `dim_product.product_code` |
| `additional_qty` | `FLOAT64` | ❌ | Additional ordering units to add to the replenishment order |
| `created_at` | `TIMESTAMP` | ❌ | Record creation timestamp |

**Primary Key:** `order_id`  
**Foreign Keys:** `pharmacy_id` → `dim_pharmacy`, `product_code` → `dim_product`

---

## Layer 4 – Mart Tables (Pipeline Output)

> These tables are **written by the pipeline** and are the primary data sources for the dashboard API.

### `mart_abc_xyz_classification`

> **Replaces:** *(previously computed in-memory only, not persisted)*  
> **Dataset:** `dw_replenishment_prod`  
> **Description:** Computed ABC-XYZ classification per pharmacy per SKU per run cycle. Persisting this enables trend analysis across replenishment cycles.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `pharmacy_id` | `STRING` | ❌ | FK → `dim_pharmacy.pharmacy_id` |
| `product_code` | `STRING` | ❌ | FK → `dim_product.product_code` |
| `cycle_id` | `STRING` | ❌ | Replenishment run identifier |
| `total_revenue` | `FLOAT64` | ❌ | Summed net revenue over the analysis window |
| `total_qty_sold` | `FLOAT64` | ❌ | Summed quantity sold over the analysis window |
| `revenue_rank` | `INTEGER` | ❌ | Revenue rank within the pharmacy (1 = highest) |
| `cumulative_revenue_pct` | `FLOAT64` | ❌ | Cumulative % of pharmacy revenue up to and including this SKU |
| `abcde_category` | `STRING` | ❌ | Revenue classification: `'A'`, `'B'`, `'C'`, `'D'`, `'E'` |
| `mean_weekly_qty` | `FLOAT64` | ✅ | Average weekly sales quantity |
| `std_weekly_qty` | `FLOAT64` | ✅ | Standard deviation of weekly sales quantity |
| `coefficient_of_variation` | `FLOAT64` | ✅ | `std / mean`. Demand variability measure. NULL if only 1 data point |
| `xyz_class` | `STRING` | ❌ | `'X'` (CV < 0.5), `'Y'` (CV < 1.0), `'Z'` (CV ≥ 1.0), `'Unknown'` |
| `abc_xyz_class` | `STRING` | ❌ | Combined classification. E.g. `'AX'`, `'BZ'`, `'CY'` |
| `computed_at` | `TIMESTAMP` | ❌ | When this record was computed |

**Primary Key:** `pharmacy_id, product_code, cycle_id`

---

### `mart_replenishment_order`

> **Replaces:** *(previously exported only to CSV/Google Sheets as `ordering_df` — not persisted in BQ)*  
> **Dataset:** `dw_replenishment_prod`  
> **Description:** The final output of the replenishment pipeline. One row per pharmacy per SKU per replenishment cycle. **Primary table for the dashboard.**

| Column | Type | Nullable | Description |
|---|---|---|---|
| **— Identity —** | | | |
| `order_row_id` | `STRING` | ❌ | Primary key. Auto-generated UUID |
| `cycle_id` | `STRING` | ❌ | Replenishment run batch identifier |
| `computed_at` | `TIMESTAMP` | ❌ | Timestamp when this row was generated |
| `pharmacy_id` | `STRING` | ❌ | FK → `dim_pharmacy.pharmacy_id` |
| `pharmacy_name` | `STRING` | ✅ | Denormalized pharmacy name |
| `product_code` | `STRING` | ❌ | FK → `dim_product.product_code` |
| **— Classification —** | | | |
| `abc_xyz_class` | `STRING` | ✅ | E.g. `'AX'`, `'BZ'` |
| `abcde_category` | `STRING` | ✅ | `'A'` through `'E'` |
| `xyz_class` | `STRING` | ✅ | `'X'`, `'Y'`, `'Z'`, `'Unknown'` |
| `cumulative_revenue_pct` | `FLOAT64` | ✅ | Cumulative revenue percentage rank within the pharmacy |
| `pog_status` | `STRING` | ✅ | Planogram eligibility. `'POG Y'` or `'No Need to Replenish'` |
| **— Forecasting & Inventory Targets —** | | | |
| `forecast_method` | `STRING` | ✅ | `'SES'`, `'Croston'`, or `'None'` |
| `forecast_weekly_qty` | `FLOAT64` | ✅ | Forecasted demand for next week (sales unit) |
| `std_weekly_qty` | `FLOAT64` | ✅ | Standard deviation of historical weekly demand |
| `coefficient_of_variation` | `FLOAT64` | ✅ | Demand variability ratio |
| `z_value` | `FLOAT64` | ✅ | Service level Z-score for this ABC-XYZ class |
| `lead_time_weeks` | `FLOAT64` | ✅ | Applied lead time used in safety stock calculation |
| `buffer_stock_qty` | `INTEGER` | ✅ | Safety stock: `CEIL(Z × σ × √(LT))` |
| `minimum_stock_qty` | `INTEGER` | ✅ | Reorder point: `CEIL(forecast_qty + buffer_stock_qty)` |
| `maximum_stock_qty` | `INTEGER` | ✅ | Order-up-to level: `CEIL(minimum_stock_qty + forecast_qty)` |
| **— Current Stock Position —** | | | |
| `on_hand_qty` | `FLOAT64` | ✅ | Current physical stock (sales unit) |
| `in_transit_qty` | `FLOAT64` | ✅ | In-transit stock from open POs (sales unit) |
| `effective_qty` | `FLOAT64` | ✅ | `on_hand_qty + in_transit_qty` |
| **— Planogram —** | | | |
| `planogram_unit_qty` | `FLOAT64` | ✅ | Target planogram shelf quantity (ordering unit) |
| `planogram_target_gap` | `FLOAT64` | ✅ | `CEIL(planogram_unit_qty - effective_qty / conversion_factor)` |
| `planogram_gap_applied` | `BOOLEAN` | ❌ | Whether planogram gap was used instead of forecast-based qty |
| **— Replenishment Logic —** | | | |
| `replenishment_mode` | `STRING` | ✅ | `'max'` or `'min'` |
| `replenishment_qty_raw` | `FLOAT64` | ✅ | Raw replenishment qty before unit conversion (sales unit) |
| `replenishment_status` | `STRING` | ✅ | Text outcome before conversion. E.g. `'Overstock'`, `'No need to replenish'`, `'No Follow'`, `'Hold'`, `'Hold to Zero'`, `'Manually Review'` |
| **— Conversion & Ordering —** | | | |
| `conversion_factor` | `FLOAT64` | ✅ | Unit conversion factor applied |
| `ordered_qty_converted` | `FLOAT64` | ✅ | `CEIL(replenishment_qty_raw / conversion_factor)` in ordering unit |
| **— TMP Warehouse Allocation —** | | | |
| `tmp_available_qty` | `FLOAT64` | ✅ | Available stock at TMP warehouse for this SKU at time of run |
| `tmp_item_status` | `STRING` | ✅ | TMP item status: `'active'`, `'not_active'`, `'discontinue'` |
| **— Final Output —** | | | |
| `ordered_qty_final` | `FLOAT64` | ❌ | Final order qty after TMP allocation and all overrides (ordering unit) |
| `unit_price` | `FLOAT64` | ✅ | Price per ordering unit from `dim_product_price` |
| `total_order_value` | `FLOAT64` | ✅ | `ordered_qty_final × unit_price` |
| **— Status Trail —** | | | |
| `status_before_planogram` | `STRING` | ✅ | `final_status` snapshot before planogram gap override |
| `final_status` | `STRING` | ❌ | Status after planogram logic. Numeric qty string or a status label |
| `final_status_after_allocation` | `STRING` | ✅ | Status after TMP stock allocation |
| **— Override Flags —** | | | |
| `hold_to_planogram_applied` | `BOOLEAN` | ❌ | Whether `ctrl_hold_to_planogram_rule` was activated |
| `is_additional_external_order` | `BOOLEAN` | ❌ | Whether qty from `ctrl_additional_order` was merged |

**Primary Key:** `order_row_id`  
**Foreign Keys:** `pharmacy_id` → `dim_pharmacy`, `product_code` → `dim_product`

---

## Naming Conventions

| Convention | Rule | Example |
|---|---|---|
| **Table prefix** | `dim_` = dimension, `fact_` = transactional, `stg_` = staging, `ref_` = reference, `ctrl_` = control config, `mart_` = output mart | `mart_replenishment_order` |
| **Column names** | `snake_case`, full English words, no abbreviations | `on_hand_qty` not `total_qty` |
| **Quantity columns** | Always end with `_qty` | `ordered_qty_final`, `buffer_stock_qty` |
| **Status columns** | Use `_status` suffix; values are `snake_case` strings | `item_status = 'not_active'` |
| **Boolean columns** | Named as a factual statement | `planogram_gap_applied`, `is_replenishment_active` |
| **Timestamps** | `_at` suffix for timestamps, `_date` for dates | `computed_at`, `snapshot_date` |
| **Primary keys** | `{entity}_id` for simple entities, `{entity}_row_id` for wide fact/mart tables | `pharmacy_id`, `order_row_id` |

---

## Migration Mapping Reference

> Use this table when updating `replen.py` to write to the new schema.

| Old Table | New Table | Old Column | New Column |
|---|---|---|---|
| `mart_windows_7_sales` | `fact_weekly_sales` | `qty` | `sales_qty` |
| `mart_windows_7_sales` | `fact_weekly_sales` | `net_amount` | `net_revenue` |
| `mart_windows_7_sales` | `fact_weekly_sales` | `item_code` | `product_code` |
| `stok_so_staging_old` | `fact_stock_position` | `total_qty` | `on_hand_qty` |
| `stok_so_staging_old` | `fact_stock_position` | `in_transit_qty` | `in_transit_qty` ✅ |
| `stok_so_staging_old` | `fact_stock_position` | `plano_follow` | `follow_planogram` |
| `stok_so_staging_old` | `fact_stock_position` | `item_code` | `product_code` |
| `tmp_stock` | `stg_tmp_warehouse_stock` | `tmp_qty` | `available_qty` |
| `tmp_stock` | `stg_tmp_warehouse_stock` | `tmp_status` | `item_status` |
| `tmp_stock` | `stg_tmp_warehouse_stock` | `item_code` | `product_code` |
| `pricelist` | `dim_product_price` | `price` | `unit_price` |
| `pricelist` | `dim_product_price` | `item_code` | `product_code` |
| `conversion_staging` | `dim_unit_conversion` | `conversion_factor` / `conversion_factor_value` | `conversion_factor` |
| `conversion_staging` | `dim_unit_conversion` | `item_code` | `product_code` |
| `phb_aktif` | `dim_pharmacy` | `replenishment_trigger` | `is_replenishment_active` |
| `gsheet_plano_qty_initial_results` | `ref_planogram` | `unit_qty` | `planogram_unit_qty` |
| `gsheet_plano_qty_initial_results` | `ref_planogram` | `item_code` | `product_code` |
| `gsheet_plano_qty_initial_results` | `ref_planogram` | `status` | `item_status` |
| `gsheet_...hold_product_stock_out` | `ctrl_hold_stockout_rule` | `sku` | `product_code` |
| `gsheet_...hold_product_stock_out` | `ctrl_hold_stockout_rule` | `applied_to` | `applicable_pharmacy_ids` |
| `gsheet_...hold_to_plano` | `ctrl_hold_to_planogram_rule` | `sku` | `product_code` |
| `gsheet_...hold_to_plano` | `ctrl_hold_to_planogram_rule` | `applied_to` | `applicable_pharmacy_ids` |
| `gsheet_...special_control` | `ctrl_replenishment_config` | `category` | `abc_xyz_category` |
| `gsheet_...special_control` | `ctrl_replenishment_config` | `tools` | `replenishment_mode` |
| `gsheet_...special_control` | `ctrl_replenishment_config` | `lead_time` | `lead_time_weeks` |
| `gsheet_...additional_order` | `ctrl_additional_order` | `qty` / `quantity` | `additional_qty` |
| `gsheet_...additional_order` | `ctrl_additional_order` | `sku` / `item_code` | `product_code` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `item_code` | `product_code` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `replenish_value` | `replenishment_qty_raw` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `converted_qty` | `ordered_qty_converted` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `total_qty` | `on_hand_qty` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `total_order_val` | `total_order_value` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `pog_status_global` | `pog_status` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `plano_unit_gap_applied` | `planogram_gap_applied` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `hold_to_plano_applied` | `hold_to_planogram_applied` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `is_additional_external` | `is_additional_external_order` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `buffer_stock` | `buffer_stock_qty` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `minimum_stock` | `minimum_stock_qty` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `maximum_stock` | `maximum_stock_qty` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `forecast_qty` | `forecast_weekly_qty` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `cv` | `coefficient_of_variation` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `abc_xyz` | `abc_xyz_class` |
| *(ordering_df — not persisted)* | `mart_replenishment_order` | `abcde_category` | `abcde_category` ✅ |
