from google.colab import auth
auth.authenticate_user()

import math
import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import gspread
from google.auth import default
import pandas as pd
from gspread_dataframe import get_as_dataframe
from google.colab import userdata

import os
import numpy as np
import pandas as pd
import gspread
import google.auth
from google.cloud import bigquery
from IPython.display import display, HTML

PROJECT_ID = "mclinica-analytics"
DATASET = "dw_swiperx_dev_waliyudin"
DRIVE_OUTPUT_PATH = "/content/drive/MyDrive/replenish"
UPLOAD_TO_SHEETS = True

credentials, project = google.auth.default()
gc = gspread.authorize(credentials)
client = bigquery.Client(project=PROJECT_ID)
print("Authenticated")

Z_VALUES = {
    "AX": 1.88, "AY": 1.65, "AZ": 1.48,
    "BX": 1.34, "BY": 1.28, "BZ": 1.23,
    "CX": 1.04, "CY": 0.84, "CZ": 0.67,
    "DX": 0.58, "DY": 0.58, "DZ": 0.58,
    "EX": 0.52, "EY": 0.52, "EZ": 0.52,
}

REPLENISH_MODE = {key: "max" for key in Z_VALUES}


def run_timed(label, func, *args, **kwargs):
    """Run one pipeline stage and print its wall-clock duration."""
    stage_start = time.perf_counter()
    try:
        return func(*args, **kwargs)
    finally:
        elapsed = time.perf_counter() - stage_start
        print(f"⏱️ {label}: {elapsed:.4f}s")




def read_dev_table(table_name):
    query = f"SELECT * FROM `{DATASET}.{table_name}`"
    return client.query(query).to_dataframe()

def run_abc_analysis(sales_df):
    print("[1/6] Running ABC Analysis (Revenue)...")
    if not pd.api.types.is_numeric_dtype(sales_df["net_amount"]) or not pd.api.types.is_numeric_dtype(sales_df["qty"]):
        sales_df = sales_df.copy()
        sales_df["net_amount"] = pd.to_numeric(sales_df["net_amount"], errors="coerce").fillna(0.0)
        sales_df["qty"] = pd.to_numeric(sales_df["qty"], errors="coerce").fillna(0.0)
    agg_df = sales_df.groupby(["pharmacy_id", "pharmacy_name", "item_code"]).agg({
        "net_amount": "sum",
        "qty": "sum",
    }).reset_index()

    # A single stable sort + vectorized grouped operations is much faster than
    # constructing one DataFrame per pharmacy.
    results = agg_df.sort_values(
        ["pharmacy_id", "net_amount"], ascending=[True, False], kind="stable"
    ).reset_index(drop=True)
    by_pharmacy = results.groupby("pharmacy_id", sort=False, observed=True)
    results["rank"] = by_pharmacy.cumcount() + 1
    totals = by_pharmacy["net_amount"].transform("sum")
    results["cum_revenue_pct"] = np.where(
        totals.gt(0), by_pharmacy["net_amount"].cumsum().div(totals), 0.0
    )
    results["abcde_category"] = np.select(
        [
            results["rank"].le(100),
            results["cum_revenue_pct"].le(0.80),
            results["cum_revenue_pct"].le(0.90),
            results["cum_revenue_pct"].le(0.97),
        ],
        ["A", "B", "C", "D"],
        default="E",
    )
    return results

def run_abc_xyz_analysis(sales_df, abc_df):
    print("[2/6] Running ABC-XYZ Volatility Analysis...")
    if not pd.api.types.is_numeric_dtype(sales_df["qty"]):
        sales_df = sales_df.copy()
        sales_df["qty"] = pd.to_numeric(sales_df["qty"], errors="coerce").fillna(0.0)
    grouped_qty = sales_df.groupby(["pharmacy_id", "item_code"])["qty"]
    stats_df = pd.DataFrame({
        "mean_qty": grouped_qty.mean(),
        "std_qty": grouped_qty.std(ddof=0),
    }).reset_index()
    stats_df["cv"] = pd.to_numeric(
        stats_df["std_qty"].div(stats_df["mean_qty"].replace(0, np.nan)),
        errors="coerce",
    ).astype("float64")

    # to_numpy(dtype=bool) makes this compatible with both regular NumPy
    # dtypes and BigQuery-backed pandas nullable dtypes.
    stats_df["xyz_class"] = np.select(
        [
            stats_df["cv"].isna().to_numpy(dtype=bool),
            stats_df["cv"].lt(0.5).to_numpy(dtype=bool),
            stats_df["cv"].lt(1.0).to_numpy(dtype=bool),
        ],
        ["Unknown", "X", "Y"], default="Z"
    )
    merged_df = abc_df.merge(
        stats_df[["pharmacy_id", "item_code", "cv", "xyz_class"]],
        on=["pharmacy_id", "item_code"],
        how="left",
    )
    merged_df.loc[merged_df["abcde_category"].isin(["D", "E"]), "xyz_class"] = "Z"
    merged_df["abc_xyz"] = merged_df["abcde_category"] + merged_df["xyz_class"]
    return merged_df

def simple_exponential_smoothing(history_list, alpha=0.5):
    clean_data = [float(x) for x in history_list if x is not None and str(x) != "" and pd.notna(x)]
    if not clean_data:
        return 0.0
    smoothed = clean_data[0]
    for actual in clean_data[1:]:
        smoothed = alpha * actual + (1.0 - alpha) * smoothed
    return float(smoothed)

def croston_gsheet_style(history_list):
    clean_data = [float(x) for x in history_list if x is not None and str(x) != "" and x >= 0]
    if not clean_data:
        return 0.0
    non_zero = [x for x in clean_data if x > 0]
    if not non_zero:
        return 0.0
    avg_demand = sum(non_zero) / len(non_zero)
    avg_interval = 1.0 if len(non_zero) <= 1 else len(clean_data) / len(non_zero)
    return avg_demand / avg_interval

def run_forecast_analysis(sales_df, abc_xyz_df):
    print("[3/6] Running Demand Forecasting...")
    if not pd.api.types.is_numeric_dtype(sales_df["qty"]):
        sales_df = sales_df.copy()
        sales_df["qty"] = pd.to_numeric(sales_df["qty"], errors="coerce").fillna(0.0)
    if "week_index" in sales_df.columns:
        sales_df = sales_df.sort_values(["pharmacy_id", "item_code", "week_index"])
    grouped_history = sales_df.groupby(
        ["pharmacy_id", "item_code"], sort=False, observed=True
    )["qty"].agg(list).to_dict()
    rows = []
    # itertuples avoids creating a pandas Series for every SKU.
    for row in abc_xyz_df[["pharmacy_id", "item_code", "abc_xyz"]].itertuples(index=False):
        key = (row.pharmacy_id, row.item_code)
        history = grouped_history.get(key, [])
        abc_xyz = str(row.abc_xyz)
        if not history:
            method, forecast_qty = "None", 0.0
        elif abc_xyz.endswith("Z"):
            method, forecast_qty = "Croston", croston_gsheet_style(history)
        else:
            method, forecast_qty = "SES", simple_exponential_smoothing(history)
        rows.append({
            "pharmacy_id": row.pharmacy_id,
            "item_code": row.item_code,
            "abc_xyz": abc_xyz,
            "forecast_method": method,
            "forecast_qty": float(forecast_qty)
        })
    return pd.DataFrame(rows)

def run_inventory_targets_analysis(sales_df, forecast_df, config_df, plano_df=None):
    print("[4/6] Running Inventory Targets Calculation...")
    if not pd.api.types.is_numeric_dtype(sales_df["qty"]):
        sales_df = sales_df.copy()
        sales_df["qty"] = pd.to_numeric(sales_df["qty"], errors="coerce").fillna(0.0)
    grouped_qty = sales_df.groupby(["pharmacy_id", "item_code"])["qty"]
    stats_df = pd.DataFrame({
        "std_qty": grouped_qty.std(ddof=0),
    }).reset_index()
    merged_df = forecast_df.merge(stats_df, on=["pharmacy_id", "item_code"], how="left")
    merged_df["std_qty"] = merged_df["std_qty"].fillna(0)
    merged_df["z_value"] = merged_df["abc_xyz"].map(Z_VALUES).fillna(0)

    # Initialize default lead time to 1.0
    merged_df["lead_time"] = 1.0

    if config_df is not None and not config_df.empty:
        config = config_df.copy()
        config.columns = [c.strip().lower().replace(" ", "_") for c in config.columns]

        for idx, row in config.iterrows():
            cat = str(row.get("category", "")).strip().upper()
            try:
                lt_val = float(row.get("lead_time", 1.0))
            except Exception:
                lt_val = 1.0

            applied_to_str = str(row.get("applied_to", "")).strip()

            if pd.isna(row.get("applied_to")) or not applied_to_str or applied_to_str.lower() in ["nan", "none", ""]:
                mask = (merged_df["abc_xyz"] == cat)
            else:
                pharmacies = [p.strip() for p in applied_to_str.split(",") if p.strip()]
                mask = (merged_df["abc_xyz"] == cat) & (merged_df["pharmacy_id"].astype(str).str.strip().isin(pharmacies))

            merged_df.loc[mask, "lead_time"] = lt_val

    # Z * Std Dev * sqrt(LT)
    merged_df["buffer_stock"] = np.ceil(
        merged_df["z_value"] * merged_df["std_qty"] * np.sqrt(merged_df["lead_time"])
    ).astype(int)
    merged_df["minimum_stock"] = np.ceil(merged_df["forecast_qty"] + merged_df["buffer_stock"]).astype(int)
    merged_df["maximum_stock"] = np.ceil(merged_df["minimum_stock"] + merged_df["forecast_qty"]).astype(int)

    if plano_df is not None and not plano_df.empty and "item_code" in plano_df.columns:
        plano_items = set(plano_df["item_code"].astype(str).str.strip())
        merged_df["pog_status_global"] = np.where(
            merged_df["item_code"].astype(str).str.strip().isin(plano_items),
            "POG Y", "No Need to Replenish"
        )
    else:
        merged_df["pog_status_global"] = "No Need to Replenish"
    return merged_df

def load_stock_and_intransit_combined_df(stock_transit_df):
    if stock_transit_df is None or stock_transit_df.empty:
        return pd.DataFrame()
    df = stock_transit_df.copy()
    cols_map = {c.strip().lower().replace(" ", "_"): c for c in df.columns}
    mapping = {
        "pharmacy_id": ["pharmacy_id", "id_pharmacy", "id", "id_apotek"],
        "pharmacy_name": ["pharmacy_name", "name", "pharmacy", "nama_apotek"],
        "item_code": ["item_code", "sku_code", "sku", "code", "kode_item"],
        "total_qty": ["quantity", "qty", "stock", "total_qty", "stok"],
        "in_transit_qty": ["in_transit", "transit", "in_transit_qty"],
        "plano_follow": ["follow", "plano_follow", "plano", "follow_plano"],
    }
    found_cols = {}
    for target, candidates in mapping.items():
        for cand in candidates:
            if cand in cols_map:
                found_cols[target] = cols_map[cand]
                break
    if not all(col in found_cols for col in ["pharmacy_id", "item_code"]):
        return pd.DataFrame()

    std_df = pd.DataFrame()
    std_df["pharmacy_id"] = df[found_cols["pharmacy_id"]].astype(str).str.strip()
    std_df["item_code"] = df[found_cols["item_code"]].astype(str).str.strip()
    std_df["pharmacy_name"] = df[found_cols["pharmacy_name"]] if "pharmacy_name" in found_cols else "Unknown"
    std_df["total_qty"] = pd.to_numeric(df[found_cols["total_qty"]].astype(str).str.replace(",", ""), errors="coerce").fillna(0) if "total_qty" in found_cols else 0.0
    std_df["in_transit_qty"] = pd.to_numeric(df[found_cols["in_transit_qty"]].astype(str).str.replace(",", ""), errors="coerce").fillna(0) if "in_transit_qty" in found_cols else 0.0
    std_df["plano_follow"] = df[found_cols["plano_follow"]] if "plano_follow" in found_cols else np.nan
    return std_df.groupby(["pharmacy_id", "pharmacy_name", "item_code"], as_index=False).agg({
        "total_qty": "sum",
        "in_transit_qty": "sum",
        "plano_follow": "last",
    })

def compute_replenish(row, plano_allowed):
    if plano_allowed:
        item_code = str(row.get("item_code", "")).strip()
        if item_code and item_code not in plano_allowed:
            return "Manually Review"
    total_qty = row.get("total_qty", 0)
    min_stock = row.get("minimum_stock", 0)
    max_stock = row.get("maximum_stock", 0)
    effective_qty = row.get("effective_qty", 0)
    if str(row.get("plano_follow", "")).strip().lower() == "no":
        return "No Follow"
    target = max_stock if str(row.get("replenish_mode", "max")).strip().lower() == "max" else min_stock
    if total_qty > target:
        return "Overstock"
    if total_qty >= min_stock:
        return "No need to replenish"
    normal_qty = max(target - effective_qty, 0)
    return "No need to replenish" if normal_qty <= 0 else round(normal_qty, 2)

def build_plano_unit_lookup(plano_df):
    required_cols = {"pharmacy_name", "item_code", "unit_qty"}
    if plano_df is None or plano_df.empty or not required_cols.issubset(plano_df.columns):
        return {}
    df = plano_df[["pharmacy_name", "item_code", "unit_qty"]].copy()
    df["pharmacy_name"] = df["pharmacy_name"].astype(str).str.strip()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["unit_qty"] = pd.to_numeric(df["unit_qty"], errors="coerce")
    df = df.dropna(subset=["pharmacy_name", "item_code", "unit_qty"])
    return df.groupby(["pharmacy_name", "item_code"])["unit_qty"].last().to_dict()

def is_number(value):
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False

def calculate_plano_target_gap(row):
    unit_qty = row.get("plano_unit_qty")
    effective_qty = row.get("effective_qty", 0)
    conversion_factor = row.get("conversion_factor")
    if not pd.notna(unit_qty) or not pd.notna(conversion_factor) or conversion_factor <= 0:
        return np.nan
    effective_qty_converted = effective_qty / conversion_factor
    target_gap = np.ceil(unit_qty - effective_qty_converted)
    return float(target_gap) if np.isfinite(target_gap) and target_gap > 0 else 0.0

def apply_plano_unit_final_status(row):
    if str(row.get("final_status_before_plano", "")).strip().lower() == "stockout tmp":
        return row.get("final_status")
    target_gap = row.get("target_gap")
    if not pd.notna(target_gap) or target_gap <= 0:
        return row.get("final_status")
    try:
        final_status_qty = float(row.get("final_status"))
    except (ValueError, TypeError):
        final_status_qty = 0.0
    if target_gap > final_status_qty:
        return float(target_gap)
    return row.get("final_status")

def preprocess_hold_rules(hold_df):
    if hold_df is None or hold_df.empty:
        return {}
    df_clean = hold_df.copy()
    df_clean.columns = [c.strip().lower().replace(" ", "_").replace("\ufeff", "") for c in df_clean.columns]

    sku_col = next((c for c in df_clean.columns if "sku" in c), None)
    type_col = next((c for c in df_clean.columns if "type" in c), None)
    applied_to_col = next((c for c in df_clean.columns if "applied" in c), None)

    if not sku_col or not type_col or not applied_to_col:
        return {}

    rules_dict = {}
    for _, row in df_clean.iterrows():
        sku = str(row.get(sku_col, "")).strip()
        rule_type = str(row.get(type_col, "")).strip()
        app_to = str(row.get(applied_to_col, "")).strip().lower()

        if not sku or not rule_type:
            continue

        if sku not in rules_dict:
            rules_dict[sku] = {}

        if app_to == "all":
            rules_dict[sku]["all"] = rule_type
        else:
            pharmacies = [p.strip() for p in app_to.split(",") if p.strip()]
            for p in pharmacies:
                rules_dict[sku][p] = rule_type
    return rules_dict

def get_hold_rule(item_code, pharmacy_id, rules_dict):
    if not rules_dict:
        return None
    sku = str(item_code).strip()
    sku_rules = rules_dict.get(sku)
    if sku_rules is None:
        return None
    p_id = str(pharmacy_id).strip()
    return sku_rules.get(p_id, sku_rules.get("all"))

def apply_hold_to_plano_rules(ordering_df, hold_to_plano_rules_dict):
    """Override forecast-based orders with the remaining planogram gap.

    The dedicated control table uses the same SKU / Type / Applied to shape as
    the stock-out hold table. Any valid row in that table activates this rule
    for the matching SKU and pharmacy scope. Deduct effective pharmacy stock
    (on-hand plus in-transit) from the planogram quantity after converting that
    stock into ordering units.

    This override runs before TMP allocation, so limited TMP stock can still
    reduce the requested planogram quantity.
    """
    if ordering_df is None or ordering_df.empty:
        return ordering_df

    result_df = ordering_df.copy()
    result_df["hold_to_plano_applied"] = False
    if not hold_to_plano_rules_dict:
        return result_df

    required_cols = {"pharmacy_id", "item_code", "ordered_qty_final"}
    if not required_cols.issubset(result_df.columns):
        return result_df

    matched_rules = pd.Series(
        [
            get_hold_rule(item_code, pharmacy_id, hold_to_plano_rules_dict)
            for item_code, pharmacy_id in zip(
                result_df["item_code"], result_df["pharmacy_id"]
            )
        ],
        index=result_df.index,
        dtype="object",
    )
    matched = matched_rules.notna()
    if not matched.any():
        return result_df

    # Do not reactivate products suppressed by master/planogram status.
    current_status = result_df.get(
        "final_status", pd.Series("", index=result_df.index)
    ).fillna("").astype(str).str.strip().str.lower()
    protected = current_status.isin(
        ["not active", "discontinue", "no follow"]
    )
    applicable = matched & ~protected

    plano_qty = pd.to_numeric(
        result_df.get("plano_unit_qty", pd.Series(np.nan, index=result_df.index)),
        errors="coerce",
    )
    effective_qty = pd.to_numeric(
        result_df.get("effective_qty", pd.Series(0.0, index=result_df.index)),
        errors="coerce",
    ).fillna(0.0)
    conversion_factor = pd.to_numeric(
        result_df.get("conversion_factor", pd.Series(np.nan, index=result_df.index)),
        errors="coerce",
    )

    valid_plano = applicable & plano_qty.notna() & conversion_factor.gt(0)
    missing_plano = applicable & plano_qty.isna()
    missing_conversion = applicable & plano_qty.notna() & ~conversion_factor.gt(0)

    order_gap = pd.Series(0.0, index=result_df.index)
    order_gap.loc[valid_plano] = np.maximum(
        np.ceil(
            plano_qty.loc[valid_plano]
            - effective_qty.loc[valid_plano] / conversion_factor.loc[valid_plano]
        ),
        0.0,
    )

    result_df.loc[valid_plano, "ordered_qty_final"] = order_gap.loc[valid_plano]
    result_df.loc[valid_plano, "hold_to_plano_applied"] = True

    if "final_status" in result_df.columns:
        result_df["final_status"] = result_df["final_status"].astype(object)
        positive_gap = valid_plano & order_gap.gt(0)
        zero_gap = valid_plano & order_gap.le(0)
        result_df.loc[positive_gap, "final_status"] = (
            order_gap.loc[positive_gap].astype(float).astype(str)
        )
        result_df.loc[zero_gap, "final_status"] = "No need to replenish"
        # A matched rule must never fall back to forecast when its planogram
        # quantity or conversion factor is unavailable.
        result_df.loc[missing_plano, "final_status"] = "Missing planogram quantity"
        result_df.loc[missing_conversion, "final_status"] = "Missing conversion factor"

    invalid_input = missing_plano | missing_conversion
    result_df.loc[invalid_input, "ordered_qty_final"] = 0.0
    result_df.loc[invalid_input, "hold_to_plano_applied"] = True

    if {"price", "total_order_val"}.issubset(result_df.columns):
        result_df["total_order_val"] = (
            pd.to_numeric(result_df["ordered_qty_final"], errors="coerce").fillna(0)
            * pd.to_numeric(result_df["price"], errors="coerce").fillna(0)
        )

    return result_df

def run_inventory_balancing(targets_df, sales_df, stock_transit_df=None, config_df=None, hold_rules_dict=None, plano_df=None):
    print("[5/6] Running Inventory Balancing (Stock/Planograms)...")
    targets_df = targets_df.copy()
    targets_df["pharmacy_id"] = targets_df["pharmacy_id"].astype(str).str.strip()
    targets_df["item_code"] = targets_df["item_code"].astype(str).str.strip()
    sales_df = sales_df.copy()
    sales_df["pharmacy_id"] = sales_df["pharmacy_id"].astype(str).str.strip()
    pharmacy_map = sales_df.drop_duplicates("pharmacy_id").set_index("pharmacy_id")["pharmacy_name"].to_dict()
    targets_df["pharmacy_name"] = targets_df["pharmacy_id"].map(pharmacy_map)

    stock_df = load_stock_and_intransit_combined_df(stock_transit_df)
    if not stock_df.empty:
        stock_df["pharmacy_id"] = stock_df["pharmacy_id"].astype(str).str.strip()
        stock_df["item_code"] = stock_df["item_code"].astype(str).str.strip()
        targets_df = targets_df.merge(stock_df, on=["pharmacy_id", "item_code"], how="left", suffixes=("", "_new"))
        if "pharmacy_name_new" in targets_df.columns:
            targets_df["pharmacy_name"] = targets_df["pharmacy_name"].fillna(targets_df["pharmacy_name_new"])
            targets_df = targets_df.drop(columns=["pharmacy_name_new"])

    for col in ["total_qty", "in_transit_qty"]:
        if col not in targets_df.columns:
            targets_df[col] = 0
        targets_df[col] = targets_df[col].fillna(0)
    if "plano_follow" not in targets_df.columns:
        targets_df["plano_follow"] = np.nan

    plano_allowed = set(plano_df["item_code"].astype(str).str.strip()) if plano_df is not None and not plano_df.empty and "item_code" in plano_df.columns else set()
    targets_df["effective_qty"] = targets_df["total_qty"] + targets_df["in_transit_qty"]

    # Initialize default mode from REPLENISH_MODE mapping
    targets_df["replenish_mode"] = targets_df["abc_xyz"].map(REPLENISH_MODE).fillna("max")

    if config_df is not None and not config_df.empty:
        config = config_df.copy()
        config.columns = [c.strip().lower().replace(" ", "_") for c in config.columns]

        for idx, row in config.iterrows():
            cat = str(row.get("category", "")).strip().upper()
            tool_val = str(row.get("tools", "max")).strip().lower()
            applied_to_str = str(row.get("applied_to", "")).strip()

            if pd.isna(row.get("applied_to")) or not applied_to_str or applied_to_str.lower() in ["nan", "none", ""]:
                mask = (targets_df["abc_xyz"] == cat)
            else:
                pharmacies = [p.strip() for p in applied_to_str.split(",") if p.strip()]
                mask = (targets_df["abc_xyz"] == cat) & (targets_df["pharmacy_id"].astype(str).str.strip().isin(pharmacies))

            targets_df.loc[mask, "replenish_mode"] = tool_val

    # Vectorized replenishment rules (this used to call Python once per row).
    item_allowed = targets_df["item_code"].isin(plano_allowed) if plano_allowed else pd.Series(True, index=targets_df.index)
    follow_no = targets_df["plano_follow"].fillna("").astype(str).str.strip().str.lower().eq("no")
    use_max = targets_df["replenish_mode"].fillna("max").astype(str).str.strip().str.lower().eq("max")
    target = np.where(use_max, targets_df["maximum_stock"], targets_df["minimum_stock"])
    normal_qty = np.maximum(target - targets_df["effective_qty"], 0)
    result = pd.Series(normal_qty, index=targets_df.index, dtype=object)
    result.loc[targets_df["total_qty"].ge(targets_df["minimum_stock"]) | (normal_qty <= 0)] = "No need to replenish"
    result.loc[targets_df["total_qty"].gt(target)] = "Overstock"
    result.loc[follow_no] = "No Follow"
    result.loc[~item_allowed] = "Manually Review"
    targets_df["replenish_value"] = result

    # Apply hold overrides to replenish_value
    if hold_rules_dict:
        def apply_hold_replenish(row):
            item_code = row.get("item_code")
            pharmacy_id = row.get("pharmacy_id")
            rule_type = get_hold_rule(item_code, pharmacy_id, hold_rules_dict)

            if rule_type is not None:
                rule_type_lower = rule_type.lower()
                if "hold to stock-out" in rule_type_lower or "hold to stock out" in rule_type_lower:
                    # Use effective_qty only to determine if stock is zero or not
                    effective_qty = float(row.get("effective_qty", 0.0))
                    if effective_qty > 0:
                        return "Hold to Zero"
                elif rule_type_lower == "hold":
                    return "Hold"
            return row.get("replenish_value")

        rules = [
            get_hold_rule(item, pharmacy, hold_rules_dict)
            for item, pharmacy in zip(targets_df["item_code"], targets_df["pharmacy_id"])
        ]
        rule_series = pd.Series(rules, index=targets_df.index, dtype="object").fillna("").str.lower()
        hold_to_zero = rule_series.str.contains(r"hold to stock[- ]out", regex=True) & targets_df["effective_qty"].gt(0)
        hold = rule_series.eq("hold")
        targets_df.loc[hold_to_zero, "replenish_value"] = "Hold to Zero"
        targets_df.loc[hold, "replenish_value"] = "Hold"

    plano_unit_lookup = build_plano_unit_lookup(plano_df)
    if plano_unit_lookup:
        keys = pd.MultiIndex.from_arrays([
            targets_df["pharmacy_name"].fillna("").astype(str).str.strip(),
            targets_df["item_code"].fillna("").astype(str).str.strip(),
        ])
        targets_df["plano_unit_qty"] = keys.map(plano_unit_lookup)
    cols = ["pharmacy_id", "pharmacy_name", "item_code", "abc_xyz", "maximum_stock", "minimum_stock", "total_qty", "in_transit_qty", "effective_qty", "plano_unit_qty", "plano_follow", "replenish_mode", "replenish_value"]
    return targets_df[[c for c in cols if c in targets_df.columns]].copy()

def load_conversion_df(conversion_df):
    if conversion_df is None or conversion_df.empty:
        return pd.DataFrame(columns=["item_code", "conversion_factor"])
    df = conversion_df.copy()
    cols = {c.strip().lower().replace(" ", "_"): c for c in df.columns}
    factor_col = next((cols[c] for c in ["conversion_factor", "conversion_factor_value", "conversion_factor_val"] if c in cols), None)
    if "item_code" not in df.columns or not factor_col:
        return pd.DataFrame(columns=["item_code", "conversion_factor"])
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["conversion_factor"] = pd.to_numeric(df[factor_col], errors="coerce")
    return df[["item_code", "conversion_factor"]].dropna(subset=["conversion_factor"])

def load_stock_tmp_df(stock_tmp_df):
    if stock_tmp_df is None or stock_tmp_df.empty:
        return pd.DataFrame(columns=["item_code", "tmp_qty", "tmp_status"])
    df = stock_tmp_df.copy()
    cols = {c.strip().lower().replace(" ", "_"): c for c in df.columns}
    item_col = next((cols[c] for c in ["item_code", "sku_code", "sku", "code"] if c in cols), None)
    qty_col = next((cols[c] for c in ["tmp_qty", "stock_tmp", "stock_tmp_qty", "qty", "quantity", "available_qty"] if c in cols), None)
    status_col = next((cols[c] for c in ["status", "item_status", "tmp_status"] if c in cols), None)
    if not item_col or not qty_col:
        return pd.DataFrame(columns=["item_code", "tmp_qty", "tmp_status"])
    rename_dict = {item_col: "item_code", qty_col: "tmp_qty"}
    if status_col:
        rename_dict[status_col] = "tmp_status"
    df = df.rename(columns=rename_dict)
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["tmp_qty"] = pd.to_numeric(df["tmp_qty"], errors="coerce")

    keep_cols = ["item_code", "tmp_qty"]
    if "tmp_status" in df.columns:
        df["tmp_status"] = df["tmp_status"].fillna("").astype(str).str.strip().str.lower()
        keep_cols.append("tmp_status")
    return df.dropna(subset=["item_code"]).drop_duplicates("item_code", keep="last")[keep_cols]

def compute_converted(row):
    try:
        val = float(row.get("replenish_value"))
    except (ValueError, TypeError):
        return 0.0
    factor = row.get("conversion_factor")
    if not pd.notna(factor) or factor <= 0:
        return 0.0
    converted = np.ceil(val / float(factor))
    return float(converted) if np.isfinite(converted) and converted > 0 else 0.0

def allocate_tmp_stock(df, hold_rules_dict=None):
    def allocate_group(group):
        group["final_status_after_allocation"] = group["final_status"]
        if "tmp_qty" not in group.columns:
            return group
        group["ordered_qty_final"] = pd.to_numeric(group["ordered_qty_final"], errors="coerce").fillna(0.0)
        available = pd.to_numeric(pd.Series([group["tmp_qty"].iloc[0]]), errors="coerce").iloc[0]
        total_need = group["ordered_qty_final"].sum()

        if pd.isna(available):
            pass
        elif available <= 0:
            group["ordered_qty_final"] = 0.0
            protected = group["final_status"].isin(["Not Active", "No Follow", "Overstock", "No need to replenish"])
            group["final_status_after_allocation"] = group["final_status"].where(protected, "Stockout TMP")
            if "plano_unit_gap_applied" in group.columns:
                group.loc[group["final_status_after_allocation"] == "Stockout TMP", "plano_unit_gap_applied"] = False
        elif total_need <= 0:
            group["ordered_qty_final"] = 0.0
        elif available >= total_need:
            pass
        else:
            shares = group["ordered_qty_final"] * (available / total_need)
            floored_shares = np.floor(shares).astype(int)
            allocated_sum = floored_shares.sum()
            remaining = int(available - allocated_sum)

            if remaining > 0:
                # Secondary sort: pharmacy_id ascending (tie-breaker when remainders are equal).
                remainders = shares - floored_shares
                pharm_ids = pd.to_numeric(group["pharmacy_id"], errors="coerce").fillna(999999) if "pharmacy_id" in group.columns else pd.Series(999999, index=group.index)
                pharm_ids.index = remainders.index
                sort_df = pd.DataFrame({"remainder": remainders, "pharmacy_id": pharm_ids})
                sorted_indices = sort_df.sort_values(["remainder", "pharmacy_id"], ascending=[False, True]).index
                for idx in sorted_indices:
                    if remaining <= 0:
                        break
                    if floored_shares.loc[idx] < group.loc[idx, "ordered_qty_final"]:
                        floored_shares.loc[idx] += 1
                        remaining -= 1

            group["ordered_qty_final"] = floored_shares.astype(float)

            numeric_status = pd.to_numeric(group["final_status"], errors="coerce").notna()
            group.loc[numeric_status, "final_status_after_allocation"] = group.loc[numeric_status, "ordered_qty_final"].astype(str)
        return group

    result_df = pd.concat([allocate_group(group.copy()) for _, group in df.groupby("item_code", observed=False)], ignore_index=True)

    # Overwrite final_status_after_allocation & ordered_qty_final based on hold config
    if hold_rules_dict:
        rules = [
            get_hold_rule(item, pharmacy, hold_rules_dict)
            for item, pharmacy in zip(result_df["item_code"], result_df["pharmacy_id"])
        ]
        rule_series = pd.Series(rules, index=result_df.index, dtype="object").fillna("").str.lower()
        hold_to_zero = rule_series.str.contains(r"hold to stock[- ]out", regex=True) & pd.to_numeric(
            result_df["effective_qty"], errors="coerce"
        ).fillna(0).gt(0)
        hold = rule_series.eq("hold")
        result_df.loc[hold_to_zero, ["final_status_after_allocation", "ordered_qty_final"]] = ["Hold to Zero", 0.0]
        result_df.loc[hold, ["final_status_after_allocation", "ordered_qty_final"]] = ["Hold", 0.0]

    return result_df


def run_inventory_ordering(balancing_df, abc_xyz_df=None, targets_df=None, conversion_df=None, stock_tmp_df=None, price_df=None):
    print("[6/6] Running Ordering and Conversions...")
    balancing_df = balancing_df.copy()
    balancing_df["item_code"] = balancing_df["item_code"].astype(str).str.strip()
    balancing_df["pharmacy_id"] = balancing_df["pharmacy_id"].astype(str).str.strip()

    if abc_xyz_df is not None and not abc_xyz_df.empty:
        abc_extra = abc_xyz_df[["pharmacy_id", "item_code", "cv", "cum_revenue_pct"]].copy()
        abc_extra["pharmacy_id"] = abc_extra["pharmacy_id"].astype(str).str.strip()
        abc_extra["item_code"] = abc_extra["item_code"].astype(str).str.strip()
        balancing_df = balancing_df.merge(abc_extra, on=["pharmacy_id", "item_code"], how="left")

    if targets_df is not None and not targets_df.empty:
        target_cols = ["pharmacy_id", "item_code", "forecast_qty", "std_qty", "z_value", "buffer_stock", "pog_status_global"]
        targets_extra = targets_df[[c for c in target_cols if c in targets_df.columns]].copy()
        targets_extra["pharmacy_id"] = targets_extra["pharmacy_id"].astype(str).str.strip()
        targets_extra["item_code"] = targets_extra["item_code"].astype(str).str.strip()
        balancing_df = balancing_df.merge(targets_extra, on=["pharmacy_id", "item_code"], how="left")

    merged_df = balancing_df.merge(load_conversion_df(conversion_df), on="item_code", how="left")
    replenish_num = pd.to_numeric(merged_df["replenish_value"], errors="coerce")
    factor = pd.to_numeric(merged_df["conversion_factor"], errors="coerce")
    valid_conversion = replenish_num.notna() & factor.gt(0)
    merged_df["converted_qty"] = 0.0
    merged_df.loc[valid_conversion, "converted_qty"] = np.maximum(
        np.ceil(replenish_num.loc[valid_conversion] / factor.loc[valid_conversion]), 0
    )
    tmp_df = load_stock_tmp_df(stock_tmp_df)
    if not tmp_df.empty:
        merged_df = merged_df.merge(tmp_df, on="item_code", how="left")

    positive_rep = replenish_num.gt(0)
    merged_df["final_status"] = merged_df["replenish_value"].astype(str)
    merged_df.loc[positive_rep, "final_status"] = merged_df.loc[positive_rep, "converted_qty"].astype(str)
    merged_df["ordered_qty_final"] = merged_df["converted_qty"]

    if "plano_unit_qty" in merged_df.columns:
        merged_df["final_status_before_plano"] = merged_df["final_status"]
        unit_qty = pd.to_numeric(merged_df["plano_unit_qty"], errors="coerce")
        effective = pd.to_numeric(merged_df["effective_qty"], errors="coerce").fillna(0)
        valid_gap = unit_qty.notna() & factor.gt(0)
        merged_df["target_gap"] = np.nan
        gaps = np.ceil(unit_qty.loc[valid_gap] - effective.loc[valid_gap] / factor.loc[valid_gap])
        merged_df.loc[valid_gap, "target_gap"] = np.maximum(gaps, 0.0)

        numeric_status = pd.to_numeric(merged_df["final_status"], errors="coerce").fillna(0)
        gap_applies = (
            merged_df["target_gap"].gt(numeric_status)
            & ~merged_df["final_status_before_plano"].str.strip().str.lower().eq("stockout tmp")
        )
        merged_df.loc[gap_applies, "final_status"] = merged_df.loc[gap_applies, "target_gap"]
        merged_df["plano_unit_gap_applied"] = merged_df["final_status"] != merged_df["final_status_before_plano"]
    else:
        merged_df["target_gap"] = np.nan
        merged_df["plano_unit_gap_applied"] = False

    # Update ordered_qty_final to reflect any planogram gap increases
    numeric_status = pd.to_numeric(merged_df["final_status"], errors="coerce").fillna(0.0)
    merged_df["ordered_qty_final"] = numeric_status.clip(lower=0.0)

    if "plano_unit_qty" in merged_df.columns:
        plano_zero_mask = pd.to_numeric(merged_df["plano_unit_qty"], errors="coerce") == 0
        merged_df.loc[plano_zero_mask, "final_status"] = "No need to replenish"
        merged_df.loc[plano_zero_mask, "ordered_qty_final"] = 0.0

    if "tmp_status" in merged_df.columns:
        not_active_mask = merged_df["tmp_status"].notna() & merged_df["tmp_status"].isin(["", "not active", "discontinue"])
        merged_df.loc[not_active_mask, "final_status"] = "Not Active"
        merged_df.loc[not_active_mask, "ordered_qty_final"] = 0.0

    if price_df is not None and not price_df.empty and {"item_code", "price"}.issubset(price_df.columns):
        prices = price_df[["item_code", "price"]].copy()
        prices["item_code"] = prices["item_code"].astype(str).str.strip()
        merged_df = merged_df.merge(prices, on="item_code", how="left")
        merged_df["price"] = merged_df["price"].fillna(0)
    else:
        merged_df["price"] = 0
    merged_df["total_order_val"] = merged_df["ordered_qty_final"] * merged_df["price"]

    cols = ["pharmacy_id", "pharmacy_name", "item_code", "abc_xyz", "pog_status_global", "cum_revenue_pct", "std_qty", "cv", "z_value", "forecast_qty", "buffer_stock", "minimum_stock", "maximum_stock", "total_qty", "in_transit_qty", "effective_qty", "plano_unit_qty", "target_gap", "replenish_mode", "replenish_value", "conversion_factor", "converted_qty", "tmp_qty", "tmp_status", "ordered_qty_final", "price", "total_order_val", "final_status_before_plano", "final_status", "final_status_after_allocation", "plano_unit_gap_applied"]
    return merged_df[[c for c in cols if c in merged_df.columns]].copy()

def append_missing_plano_items(ordering_df, plano_df, price_df=None, stock_tmp_df=None, stock_transit_df=None):
    if plano_df is None or plano_df.empty or "item_code" not in plano_df.columns or "unit_qty" not in plano_df.columns:
        return ordering_df

    result_df = ordering_df.copy()
    plano_items = plano_df.copy()
    plano_items["item_code"] = plano_items["item_code"].astype(str).str.strip()
    plano_items["unit_qty"] = pd.to_numeric(plano_items["unit_qty"], errors="coerce")
    if "pharmacy_id" in plano_items.columns:
        plano_items["pharmacy_id"] = plano_items["pharmacy_id"].astype(str).str.strip()
    if "pharmacy_name" in plano_items.columns:
        plano_items["pharmacy_name"] = plano_items["pharmacy_name"].astype(str).str.strip()
    plano_items = plano_items.dropna(subset=["item_code", "unit_qty"])

    key_cols = ["pharmacy_id", "item_code"] if "pharmacy_id" in plano_items.columns and "pharmacy_id" in result_df.columns else ["pharmacy_name", "item_code"]
    if not all(col in plano_items.columns for col in key_cols) or not all(col in result_df.columns for col in key_cols):
        return result_df

    for col in key_cols:
        result_df[col] = result_df[col].astype(str).str.strip()
        plano_items[col] = plano_items[col].astype(str).str.strip()

    existing_keys = pd.MultiIndex.from_frame(result_df[key_cols].drop_duplicates())
    plano_keys = pd.MultiIndex.from_frame(plano_items[key_cols])
    missing_mask = ~plano_keys.isin(existing_keys)
    missing_items = plano_items.loc[missing_mask].drop_duplicates(key_cols, keep="last")
    if missing_items.empty:
        return result_df

    stock_df = load_stock_and_intransit_combined_df(stock_transit_df)
    if not stock_df.empty and all(col in stock_df.columns for col in key_cols):
        for col in key_cols:
            stock_df[col] = stock_df[col].astype(str).str.strip()

    tmp_qty_map = {}
    tmp_status_map = {}
    if stock_tmp_df is not None and not stock_tmp_df.empty:
        tmp_df = load_stock_tmp_df(stock_tmp_df)
        if not tmp_df.empty:
            tmp_qty_map = tmp_df.drop_duplicates("item_code", keep="last").set_index("item_code")["tmp_qty"].to_dict()
            if "tmp_status" in tmp_df.columns:
                tmp_status_map = tmp_df.drop_duplicates("item_code", keep="last").set_index("item_code")["tmp_status"].to_dict()

    added_rows = pd.DataFrame(index=missing_items.index, columns=result_df.columns)
    for col in ["pharmacy_id", "pharmacy_name", "item_code"]:
        if col in added_rows.columns and col in missing_items.columns:
            added_rows[col] = missing_items[col]
    if "plano_unit_qty" in added_rows.columns:
        added_rows["plano_unit_qty"] = missing_items["unit_qty"]

    # Merge stock details
    if not stock_df.empty:
        added_rows = added_rows.drop(columns=["total_qty", "in_transit_qty"], errors="ignore")
        added_rows = added_rows.merge(stock_df[key_cols + ["total_qty", "in_transit_qty"]], on=key_cols, how="left")
    added_rows["total_qty"] = pd.to_numeric(added_rows.get("total_qty"), errors="coerce").fillna(0.0)
    added_rows["in_transit_qty"] = pd.to_numeric(added_rows.get("in_transit_qty"), errors="coerce").fillna(0.0)
    added_rows["effective_qty"] = added_rows["total_qty"] + added_rows["in_transit_qty"]

    # Merge conversion factors
    conv_df = load_conversion_df(conversion_df)
    if not conv_df.empty:
        added_rows = added_rows.drop(columns=["conversion_factor"], errors="ignore")
        added_rows = added_rows.merge(conv_df, on="item_code", how="left")

    # Compute correct target gap using stock levels & conversion factors.
    # Always deduct effective_qty (converted) from plano_unit_qty — never use raw plano qty.
    if "target_gap" in added_rows.columns:
        added_rows["target_gap"] = added_rows.apply(calculate_plano_target_gap, axis=1)

    # For rows where target_gap is still NaN (missing conversion factor), attempt a
    # best-effort deduction of effective_qty directly in sales-unit terms before giving up.
    # This prevents falling back to the raw plano_unit_qty as if effective stock were zero.
    if "target_gap" in added_rows.columns:
        gap_nan_mask = added_rows["target_gap"].isna()
        if gap_nan_mask.any():
            _plano = pd.to_numeric(added_rows.loc[gap_nan_mask, "plano_unit_qty"], errors="coerce")
            _eff   = pd.to_numeric(added_rows.loc[gap_nan_mask, "effective_qty"], errors="coerce").fillna(0.0)
            _conv  = pd.to_numeric(added_rows.loc[gap_nan_mask, "conversion_factor"], errors="coerce")
            # Where conversion factor exists use the converted gap; otherwise leave as NaN
            # (these rows will be shown as "Missing conversion factor" downstream).
            valid_conv = _plano.notna() & _conv.gt(0)
            added_rows.loc[gap_nan_mask & valid_conv, "target_gap"] = np.maximum(
                np.ceil(_plano.loc[valid_conv] - _eff.loc[valid_conv] / _conv.loc[valid_conv]), 0.0
            )

    if "tmp_qty" in added_rows.columns:
        added_rows["tmp_qty"] = added_rows["item_code"].map(tmp_qty_map)
    if "tmp_status" in added_rows.columns:
        added_rows["tmp_status"] = added_rows["item_code"].map(tmp_status_map)

    # TMP stockout mask — computed here and applied after setting initial final_status below.
    stockout_tmp_mask = pd.to_numeric(added_rows.get("tmp_qty"), errors="coerce").le(0).fillna(False)

    if "ordered_qty_final" in added_rows.columns:
        # Use target_gap (which already deducts effective_qty) as the order quantity.
        # Do NOT fall back to raw plano_unit_qty — a NaN target_gap means data is
        # incomplete and will be flagged via final_status.
        added_rows["ordered_qty_final"] = added_rows["target_gap"].fillna(0.0)
    if "final_status" in added_rows.columns:
        added_rows["final_status"] = added_rows["target_gap"].fillna("Missing conversion factor")
    if "final_status_before_plano" in added_rows.columns:
        added_rows["final_status_before_plano"] = "Missing from sales"
    if "plano_unit_gap_applied" in added_rows.columns:
        added_rows["plano_unit_gap_applied"] = True

    # Apply TMP stockout: if TMP has no stock, these new rows should also be capped.
    if stockout_tmp_mask.any():
        if "ordered_qty_final" in added_rows.columns:
            added_rows.loc[stockout_tmp_mask, "ordered_qty_final"] = 0.0
        if "final_status" in added_rows.columns:
            added_rows["final_status"] = added_rows["final_status"].astype(object)
            added_rows.loc[stockout_tmp_mask, "final_status"] = "Stockout TMP"
        if "plano_unit_gap_applied" in added_rows.columns:
            added_rows.loc[stockout_tmp_mask, "plano_unit_gap_applied"] = False



    if "plano_unit_qty" in added_rows.columns:
        plano_zero_mask = pd.to_numeric(added_rows["plano_unit_qty"], errors="coerce") == 0
        if plano_zero_mask.any():
            if "ordered_qty_final" in added_rows.columns:
                added_rows.loc[plano_zero_mask, "ordered_qty_final"] = 0.0
            if "final_status" in added_rows.columns:
                added_rows["final_status"] = added_rows["final_status"].astype(object)
                added_rows.loc[plano_zero_mask, "final_status"] = "No need to replenish"
            if "final_status_before_plano" in added_rows.columns:
                added_rows["final_status_before_plano"] = added_rows["final_status_before_plano"].astype(object)
                added_rows.loc[plano_zero_mask, "final_status_before_plano"] = "No need to replenish"
            if "plano_unit_gap_applied" in added_rows.columns:
                added_rows.loc[plano_zero_mask, "plano_unit_gap_applied"] = False

    if "tmp_status" in added_rows.columns:
        not_active_mask = added_rows["tmp_status"].notna() & added_rows["tmp_status"].isin(["", "not active", "discontinue"])
        if not_active_mask.any():
            if "ordered_qty_final" in added_rows.columns:
                added_rows.loc[not_active_mask, "ordered_qty_final"] = 0.0
            if "final_status" in added_rows.columns:
                added_rows["final_status"] = added_rows["final_status"].astype(object)
                added_rows.loc[not_active_mask, "final_status"] = "Not Active"
            if "final_status_before_plano" in added_rows.columns:
                added_rows["final_status_before_plano"] = added_rows["final_status_before_plano"].astype(object)
                added_rows.loc[not_active_mask, "final_status_before_plano"] = "Not Active"

    if "price" in added_rows.columns:
        added_rows["price"] = 0.0
        if price_df is not None and not price_df.empty and {"item_code", "price"}.issubset(price_df.columns):
            prices = price_df[["item_code", "price"]].copy()
            prices["item_code"] = prices["item_code"].astype(str).str.strip()
            prices["price"] = pd.to_numeric(prices["price"], errors="coerce").fillna(0)
            price_map = prices.drop_duplicates("item_code", keep="last").set_index("item_code")["price"]
            added_rows["price"] = added_rows["item_code"].map(price_map).fillna(0).to_numpy()
    if "total_order_val" in added_rows.columns:
        added_rows["total_order_val"] = pd.to_numeric(added_rows.get("ordered_qty_final"), errors="coerce").fillna(0) * pd.to_numeric(added_rows.get("price", 0), errors="coerce").fillna(0)

    return pd.concat([result_df, added_rows], ignore_index=True)

def apply_plano_status_override(ordering_df, plano_df):
    if plano_df is None or plano_df.empty or "item_code" not in plano_df.columns:
        return ordering_df

    status_col = next((col for col in plano_df.columns if col.strip().lower() == "status"), None)
    if not status_col or "final_status" not in ordering_df.columns:
        return ordering_df

    result_df = ordering_df.copy()
    plano_status = plano_df.copy()
    plano_status["item_code"] = plano_status["item_code"].astype(str).str.strip()
    plano_status["_plano_status"] = plano_status[status_col].fillna("").astype(str).str.strip()
    plano_status["_plano_status_norm"] = plano_status["_plano_status"].str.lower()
    plano_status = plano_status[plano_status["_plano_status_norm"].isin({"not active", "discontinue", ""})].copy()
    if plano_status.empty:
        return result_df

    if "pharmacy_id" in plano_status.columns:
        plano_status["pharmacy_id"] = plano_status["pharmacy_id"].astype(str).str.strip()
    if "pharmacy_name" in plano_status.columns:
        plano_status["pharmacy_name"] = plano_status["pharmacy_name"].astype(str).str.strip()

    key_cols = ["pharmacy_id", "item_code"] if "pharmacy_id" in plano_status.columns and "pharmacy_id" in result_df.columns else ["pharmacy_name", "item_code"]
    if not all(col in plano_status.columns for col in key_cols) or not all(col in result_df.columns for col in key_cols):
        return result_df

    for col in key_cols:
        result_df[col] = result_df[col].astype(str).str.strip()
        plano_status[col] = plano_status[col].astype(str).str.strip()

    status_map = plano_status.drop_duplicates(key_cols, keep="last").set_index(key_cols)["_plano_status"]
    status_map = status_map.replace("", "Not Active")

    result_index = pd.MultiIndex.from_frame(result_df[key_cols])
    override_status = result_index.map(status_map)
    override_mask = pd.Series(override_status, index=result_df.index).notna()
    if override_mask.any():
        result_df["final_status"] = result_df["final_status"].astype(object)
        result_df.loc[override_mask, "final_status"] = pd.Series(override_status, index=result_df.index).loc[override_mask]
        if "ordered_qty_final" in result_df.columns:
            is_inactive = pd.Series(override_status, index=result_df.index).str.lower().isin(["not active", "discontinue"])
            result_df.loc[override_mask & is_inactive, "ordered_qty_final"] = 0.0
    return result_df

def integrate_additional_external_orders(ordering_df, additional_df, price_df, sales_df):
    if "is_additional_external" not in ordering_df.columns:
        ordering_df["is_additional_external"] = "No"

    if additional_df is None or additional_df.empty:
        return ordering_df

    df_clean = additional_df.copy()
    df_clean.columns = [c.strip().lower().replace(" ", "_").replace("\ufeff", "") for c in df_clean.columns]

    p_col = next((c for c in df_clean.columns if "pharmacy" in c or "pharmacies" in c), None)
    sku_col = next((c for c in df_clean.columns if "sku" in c or "item_code" in c), None)
    qty_col = next((c for c in df_clean.columns if "qty" in c or "quantity" in c), None)

    if not p_col or not sku_col or not qty_col:
        print("Warning: Could not resolve columns in additional_order table. Skipping integration.")
        return ordering_df

    # Create maps for lookup
    pharmacy_map = {}
    if sales_df is not None and not sales_df.empty:
        sales_df_clean = sales_df.copy()
        sales_df_clean["pharmacy_id"] = sales_df_clean["pharmacy_id"].astype(str).str.strip()
        pharmacy_map = sales_df_clean.drop_duplicates("pharmacy_id").set_index("pharmacy_id")["pharmacy_name"].to_dict()

    if "pharmacy_name" in ordering_df.columns:
        pharmacy_map.update(ordering_df.drop_duplicates("pharmacy_id").set_index("pharmacy_id")["pharmacy_name"].to_dict())

    price_map = {}
    if price_df is not None and not price_df.empty:
        price_df_clean = price_df.copy()
        price_df_clean["item_code"] = price_df_clean["item_code"].astype(str).str.strip()
        price_map = price_df_clean.drop_duplicates("item_code").set_index("item_code")["price"].to_dict()

    result_df = ordering_df.copy()

    result_df["pharmacy_id_str"] = result_df["pharmacy_id"].astype(str).str.strip()
    result_df["item_code_str"] = result_df["item_code"].astype(str).str.strip()

    index_map = result_df.groupby(["pharmacy_id_str", "item_code_str"]).indices

    new_rows = []

    for _, row in df_clean.iterrows():
        p_id = str(row.get(p_col, "")).strip()
        item = str(row.get(sku_col, "")).strip()
        try:
            add_qty = float(row.get(qty_col, 0))
        except (ValueError, TypeError):
            add_qty = 0.0

        if add_qty <= 0 or not p_id or not item:
            continue

        key = (p_id, item)
        if key in index_map:
            idx = index_map[key][0]
            curr_qty = pd.to_numeric(result_df.loc[idx, "ordered_qty_final"], errors="coerce")
            if pd.isna(curr_qty):
                curr_qty = 0.0
            new_qty = curr_qty + add_qty
            result_df.loc[idx, "ordered_qty_final"] = new_qty
            result_df.loc[idx, "final_status_after_allocation"] = str(new_qty)
            result_df.loc[idx, "is_additional_external"] = "Yes"
        else:
            p_name = pharmacy_map.get(p_id, "Unknown Pharmacy")
            item_price = float(price_map.get(item, 0.0))
            new_row = {col: np.nan for col in result_df.columns}
            new_row.update({
                "pharmacy_id": p_id,
                "pharmacy_name": p_name,
                "item_code": item,
                "ordered_qty_final": add_qty,
                "final_status": str(add_qty),
                "final_status_after_allocation": str(add_qty),
                "is_additional_external": "Yes",
                "price": item_price,
                "total_order_val": add_qty * item_price
            })
            new_rows.append(new_row)

    if new_rows:
        result_df = pd.concat([result_df, pd.DataFrame(new_rows)], ignore_index=True)

    result_df = result_df.drop(columns=["pharmacy_id_str", "item_code_str"])
    return result_df

def build_final_ordering_summary(ordering_df):
    summary_cols = {
        "pharmacy_id": "Pharmacy ID",
        "pharmacy_name": "Pharmacy Name",
        "item_code": "SKU ID",
        "ordered_qty_final": "Quantity",
        "total_order_val": "total order value",
    }
    if ordering_df.empty or "ordered_qty_final" not in ordering_df.columns:
        return pd.DataFrame(columns=list(summary_cols.values()))
    summary_df = ordering_df[ordering_df["ordered_qty_final"] > 0][list(summary_cols.keys())].copy()
    return summary_df.rename(columns=summary_cols)

def clean_for_sheets(df):
    clean_df = df.copy().replace([np.inf, -np.inf], np.nan)
    for col in clean_df.columns:
        if pd.api.types.is_datetime64_any_dtype(clean_df[col]):
            clean_df[col] = clean_df[col].astype(str)
    return clean_df.astype(object).where(pd.notnull(clean_df), "")

def upload_dataframe_to_sheet(df, spreadsheet_name, worksheet_name):
    try:
        spreadsheet = gc.open(spreadsheet_name)
    except gspread.SpreadsheetNotFound:
        spreadsheet = gc.create(spreadsheet_name)
        print(f"Created new spreadsheet: {spreadsheet_name}")
    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows="1", cols="1")
        print(f"Created new worksheet: {worksheet_name}")
    clean_df = clean_for_sheets(df)
    worksheet.clear()
    worksheet.update([clean_df.columns.tolist()] + clean_df.values.tolist())
    print(f"Uploaded {spreadsheet_name} / {worksheet_name}: {spreadsheet.url}")
    return spreadsheet.url
start = time.perf_counter()
print("--- 1. DATA INGESTION (PARALLEL) ---")

load_queries = {
    "price_df": f"SELECT * FROM `{DATASET}.pricelist`",
    "conversion_df": f"SELECT * FROM `{DATASET}.conversion_staging`",
    # "hold_df": "SELECT * FROM `dw_spreadsheet_prod.gsheet_inofarma_control_center_hold_product_stock_out`",
   # "hold_df": "SELECT CAST(NULL AS STRING) AS sku, CAST(NULL AS STRING) AS type, CAST(NULL AS STRING) AS applied_to WHERE FALSE",
    "hold_to_plano_df": "SELECT * FROM `dw_spreadsheet_prod.gsheet_inofarma_control_center_hold_product_stock_out`",
    "config_df": "SELECT * FROM `dw_spreadsheet_prod.gsheet_inofarma_control_center_special_control`",
    "stock_tmp_df": f"SELECT * FROM `{DATASET}.tmp_stock`",
    "store_df": f"SELECT * FROM `{PROJECT_ID}.{DATASET}.phb_aktif`",
    "additional_df": "SELECT * FROM `dw_spreadsheet_prod.gsheet_inofarma_control_center_additional_order`",
    "stock_transit_df": f"SELECT * FROM `{DATASET}.stok_so_staging_old`",
    "plano_df": "SELECT * FROM `dw_spreadsheet_prod.gsheet_plano_qty_initial_results`",
    "data_raw": f"SELECT * FROM `{DATASET}.mart_windows_7_sales`",
}

def load_bigquery_dataframe(query):
    return client.query(query).to_dataframe()

ingestion_start = time.perf_counter()
loaded = {}
optional_tables = {
    "hold_df", "hold_to_plano_df", "config_df", "additional_df", "store_df"
}
with ThreadPoolExecutor(max_workers=len(load_queries)) as executor:
    futures = {
        executor.submit(load_bigquery_dataframe, query): name
        for name, query in load_queries.items()
    }
    for future in as_completed(futures):
        name = futures[future]
        try:
            loaded[name] = future.result()
        except Exception as exc:
            if name not in optional_tables:
                raise
            print(f"Warning: Could not load {name}; using empty default. Error: {exc}")
            loaded[name] = pd.DataFrame()
        print(f"⏱️ {name}: {time.perf_counter() - ingestion_start:.4f}s")

print(f"⏱️ total_parallel_ingestion: {time.perf_counter() - ingestion_start:.4f}s")
data_raw = loaded["data_raw"]
plano_df = loaded["plano_df"]
stock_transit_df = loaded["stock_transit_df"]
conversion_df = loaded["conversion_df"]
stock_tmp_df = loaded["stock_tmp_df"]
price_df = loaded["price_df"]
config_df = loaded["config_df"]
#hold_df = loaded["hold_df"]
hold_df = pd.DataFrame(columns=["sku", "type", "applied_to"])
hold_to_plano_df = loaded["hold_to_plano_df"]
additional_df = loaded["additional_df"]
store_df = loaded["store_df"]

# Preprocess hold rules into a lookup dictionary
hold_rules_dict = run_timed("preprocess_hold_rules", preprocess_hold_rules, hold_df)
hold_to_plano_rules_dict = run_timed(
    "preprocess_hold_to_plano_rules",
    preprocess_hold_rules,
    hold_to_plano_df,
)

abc_results = run_timed("run_abc_analysis", run_abc_analysis, data_raw)
abc_xyz = run_timed("run_abc_xyz_analysis", run_abc_xyz_analysis, data_raw, abc_results)
forecast_df = run_timed("run_forecast_analysis", run_forecast_analysis, data_raw, abc_xyz)
target_df = run_timed("run_inventory_targets_analysis", run_inventory_targets_analysis, data_raw, forecast_df, config_df, plano_df)
balancing_df = run_timed("run_inventory_balancing", run_inventory_balancing, target_df, data_raw, stock_transit_df, config_df, hold_rules_dict, plano_df)
ordering_df = run_timed("run_inventory_ordering", run_inventory_ordering, balancing_df, abc_xyz, target_df, conversion_df, stock_tmp_df, price_df)
ordering_df = run_timed("append_missing_plano_items", append_missing_plano_items, ordering_df, plano_df, price_df, stock_tmp_df, stock_transit_df)
ordering_df = run_timed("apply_plano_status_override", apply_plano_status_override, ordering_df, plano_df)
ordering_df = run_timed(
    "apply_hold_to_plano_rules",
    apply_hold_to_plano_rules,
    ordering_df,
    hold_to_plano_rules_dict,
)

try:
    if store_df.empty:
        raise ValueError("store_df is empty")
    store_df.columns = [c.strip().lower().replace(" ", "_").replace("\ufeff", "") for c in store_df.columns]

    id_col = next((c for c in store_df.columns if "pharmacy_id" in c or "id_pharmacy" in c or "apotek" in c), "pharmacy_id")
    trigger_col = next((c for c in store_df.columns if "replenishment" in c and "trigger" in c), "replenishment_trigger")

    active_stores = store_df[store_df[trigger_col].astype(str).str.strip().str.lower() == "yes"]
    ACTIVE_PHB = set(active_stores[id_col].astype(str).str.strip())
except Exception as e:
    print(f"Warning: Could not load active pharmacy list. Using hardcoded backup. Error: {e}")
    ACTIVE_PHB = {
        "14001", "14002", "14003", "14004", "14005",  "14007", "14008",
        "14009",  "14011", "14012", "14013", "14014", "14015", "14016",
        "14017", "14018", "14019", "14020", "14021",
        "14025", "14029", "14030", "14034"
    }

def filter_active_stores(df, active_pharmacies):
    return df[df["pharmacy_id"].astype(str).str.strip().isin(active_pharmacies)].reset_index(drop=True)

ordering_df = run_timed("store_filtering", filter_active_stores, ordering_df, ACTIVE_PHB)

# Run the stock allocation logic on only these active pharmacies combined
ordering_df = run_timed("allocate_tmp_stock", allocate_tmp_stock, ordering_df, hold_rules_dict)

# Integrate additional external orders
ordering_df = run_timed("integrate_additional_external_orders", integrate_additional_external_orders, ordering_df, additional_df, price_df, data_raw)

# Recalculate order values based on final allocated quantities
def recalculate_order_values(df):
    if "price" in df.columns and "ordered_qty_final" in df.columns:
        df["total_order_val"] = df["ordered_qty_final"] * df["price"]
    return df

ordering_df = run_timed("value_recalculation", recalculate_order_values, ordering_df)

sy_df = run_timed("build_final_ordering_summary", build_final_ordering_summary, ordering_df)

os.makedirs(DRIVE_OUTPUT_PATH, exist_ok=True)

# 2. Define file paths
ordering_csv_path = f"{DRIVE_OUTPUT_PATH}/ordering_df.csv"
summary_csv_path = f"{DRIVE_OUTPUT_PATH}/ordering_summary.csv"

# 3. Save the dataframes into CSV files
def export_csv_files():
    ordering_df.to_csv(ordering_csv_path, index=False)
    sy_df.to_csv(summary_csv_path, index=False)

run_timed("export_csv", export_csv_files)

from google.colab import files
# files.download(summary_csv_path)

if UPLOAD_TO_SHEETS:
    def upload_and_display_summary():
        summary_url = upload_dataframe_to_sheet(
            sy_df,
            "Ordering Replenish",
            "Summary Data PHB ID"
        )
        display(HTML(f'<a href="{summary_url}" target="_blank">Open Ordering Summary</a>'))

    run_timed("upload_to_sheets", upload_and_display_summary)


files.download(ordering_csv_path)

print(f"\n🚀 TOTAL PROCESS EXECUTION TIME: {time.perf_counter() - start:.4f} seconds")

