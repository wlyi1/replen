import os
import sys
import time
import math
import numpy as np
import pandas as pd


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


# Load Z-Values mapping for ABC-XYZ categories
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

# Stage 1: ABC Analysis
def run_abc_analysis(sales_df):
    print("[1/6] Running ABC Analysis (Revenue)...")
    sales_df = sales_df.copy()
    sales_df["net_amount"] = pd.to_numeric(sales_df["net_amount"], errors="coerce").fillna(0.0)
    sales_df["qty"] = pd.to_numeric(sales_df["qty"], errors="coerce").fillna(0.0)
    
    agg_df = sales_df.groupby(["pharmacy_id", "pharmacy_name", "item_code"]).agg({
        "net_amount": "sum",
        "qty": "sum",
    }).reset_index()

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

# Stage 2: ABC-XYZ Analysis
def run_abc_xyz_analysis(sales_df, abc_df):
    print("[2/6] Running ABC-XYZ Volatility Analysis...")
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

# Time-Series Algorithms
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

# Stage 3: Forecasting
def run_forecast_analysis(sales_df, abc_xyz_df):
    print("[3/6] Running Demand Forecasting...")
    sales_df = sales_df.copy()
    sales_df["qty"] = pd.to_numeric(sales_df["qty"], errors="coerce").fillna(0.0)
    if "week_index" in sales_df.columns:
        sales_df = sales_df.sort_values(["pharmacy_id", "item_code", "week_index"])
    grouped_history = sales_df.groupby(
        ["pharmacy_id", "item_code"], sort=False, observed=True
    )["qty"].agg(list).to_dict()
    rows = []
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

# Stage 4: Targets
def run_inventory_targets_analysis(sales_df, forecast_df, config_df, plano_df=None, lt_override_df=None):
    print("[4/6] Running Inventory Targets Calculation...")
    sales_df = sales_df.copy()
    sales_df["qty"] = pd.to_numeric(sales_df["qty"], errors="coerce").fillna(0.0)
    grouped_qty = sales_df.groupby(["pharmacy_id", "item_code"])["qty"]
    stats_df = pd.DataFrame({
        "mean_qty": grouped_qty.mean(),
        "std_qty": grouped_qty.std(ddof=0),
    }).reset_index()
    merged_df = forecast_df.merge(stats_df, on=["pharmacy_id", "item_code"], how="left")
    merged_df["mean_qty"] = merged_df["mean_qty"].fillna(0).round(2)
    # std_qty/forecast_qty feed directly into the buffer/min/max formulas below — kept at full
    # float precision (no .round(2)) per BR request, so ceil() sees the true computed value
    # instead of a pre-rounded approximation. mean_qty isn't a formula input (only used for the
    # CV ratio in the unrelated ABC-XYZ stage), so it's left rounded for display purposes.
    merged_df["std_qty"] = merged_df["std_qty"].fillna(0)
    merged_df["forecast_qty"] = merged_df["forecast_qty"].fillna(0)
    merged_df["z_value"] = merged_df["abc_xyz"].map(Z_VALUES).fillna(0)

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

    # Item-level LT override — most specific tier, wins over the class-based config.csv rule above.
    # A SKU with its own saved LT (from the dashboard's edit mode) does NOT follow its ABC-XYZ class
    # rule anymore; this loop runs after the class loop so it always has the final say per row.
    if lt_override_df is not None and not lt_override_df.empty:
        override = lt_override_df.copy()
        override.columns = [c.strip().lower().replace(" ", "_") for c in override.columns]
        if {"pharmacy_id", "item_code", "lead_time"}.issubset(override.columns):
            merged_pid = merged_df["pharmacy_id"].astype(str).str.strip()
            merged_item = merged_df["item_code"].astype(str).str.strip()
            for _, row in override.iterrows():
                try:
                    lt_val = float(row["lead_time"])
                except (ValueError, TypeError):
                    continue
                pid = str(row["pharmacy_id"]).strip()
                item = str(row["item_code"]).strip()
                if not pid or not item:
                    continue
                mask = (merged_pid == pid) & (merged_item == item)
                merged_df.loc[mask, "lead_time"] = lt_val

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

# Helper: Stock & In Transit
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
    # stock_transit.csv has many blank/NaN pharmacy_id rows, forcing the whole column to float64 —
    # a plain .astype(str) here produced "14001.0" instead of "14001", which then NEVER matches
    # target_df's clean string IDs during the merge in run_inventory_balancing(). This silently
    # zeroed out total_qty/in_transit_qty for EVERY row in the entire output (100% of 59,180 rows
    # showed total_qty=0, caught by the user noticing "Stock" was 0 despite stock_transit.csv
    # clearly having 170 for that exact pharmacy+SKU). Same fix as build_plano_unit_lookup() and
    # the inactive-pharmacy exclusion filter: coerce through nullable Int64 before stringifying.
    numeric_pid = pd.to_numeric(df[found_cols["pharmacy_id"]], errors="coerce").astype("Int64")
    std_df["pharmacy_id"] = numeric_pid.astype(str).where(numeric_pid.notna(), "")
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

def build_plano_unit_lookup(plano_df):
    """{(pharmacy_id, item_code): unit_qty} from plano.csv — keyed by pharmacy_id rather than
    pharmacy_name (unlike the replen_v2.py reference) since pharmacy_id is the stripped/normalized
    join key used everywhere else in this script; plano.csv's pharmacy_name column has stray
    whitespace/casing issues that make it an unreliable key."""
    required = {"pharmacy_id", "item_code", "unit_qty"}
    if plano_df is None or plano_df.empty or not required.issubset(plano_df.columns):
        return {}
    df = plano_df[["pharmacy_id", "item_code", "unit_qty"]].copy()
    # plano.csv has some blank pharmacy_id rows, which forces the whole column to float64 (NaN
    # requires it) — .astype(str) on that produces "14001.0", not "14001", silently breaking every
    # join against merged["pharmacy_id"] (a clean string everywhere else in this script). Coerce to
    # numeric and drop the decimal BEFORE stringifying.
    df["pharmacy_id"] = pd.to_numeric(df["pharmacy_id"], errors="coerce")
    df = df.dropna(subset=["pharmacy_id"])
    df["pharmacy_id"] = df["pharmacy_id"].astype("int64").astype(str)
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["unit_qty"] = pd.to_numeric(df["unit_qty"], errors="coerce")
    df = df.dropna(subset=["item_code", "unit_qty"])
    return df.groupby(["pharmacy_id", "item_code"])["unit_qty"].last().to_dict()

def get_hold_rule(item_code, pharmacy_id, rules_dict):
    if not rules_dict:
        return None
    sku = str(item_code).strip()
    sku_rules = rules_dict.get(sku)
    if sku_rules is None:
        return None
    p_id = str(pharmacy_id).strip()
    return sku_rules.get(p_id, sku_rules.get("all"))

# Stage 5: Balancing
def run_inventory_balancing(target_df, sales_df, stock_transit_df, config_df, hold_rules_dict=None, plano_df=None):
    print("[5/6] Running Inventory Balancing...")
    stk_df = load_stock_and_intransit_combined_df(stock_transit_df)
    if stk_df.empty:
        merged = target_df.copy()
        merged["pharmacy_name"] = "Unknown"
        merged["total_qty"] = 0.0
        merged["in_transit_qty"] = 0.0
        merged["plano_follow"] = np.nan
    else:
        merged = target_df.merge(stk_df, on=["pharmacy_id", "item_code"], how="left")
        merged["total_qty"] = merged["total_qty"].fillna(0)
        merged["in_transit_qty"] = merged["in_transit_qty"].fillna(0)

    # L7D (1 week) & L30D (4 weeks) sales calculation
    sales_clean = sales_df.copy()
    sales_clean["qty"] = pd.to_numeric(sales_clean["qty"], errors="coerce").fillna(0.0)
    if "week_index" in sales_clean.columns:
        max_week = sales_clean["week_index"].max()
        l7d_sales_df = sales_clean[sales_clean["week_index"] == max_week]
        min_w_30d = max_week - 3
        l30d_sales_df = sales_clean[sales_clean["week_index"] >= min_w_30d]
    else:
        l7d_sales_df = sales_clean
        l30d_sales_df = sales_clean

    sales_7d = l7d_sales_df.groupby(["pharmacy_id", "item_code"])["qty"].sum().reset_index()
    sales_7d.rename(columns={"qty": "l7d_sales"}, inplace=True)
    merged = merged.merge(sales_7d[["pharmacy_id", "item_code", "l7d_sales"]], on=["pharmacy_id", "item_code"], how="left")
    merged["l7d_sales"] = merged["l7d_sales"].fillna(0.0)

    sales_30d = l30d_sales_df.groupby(["pharmacy_id", "item_code"])["qty"].sum().reset_index()
    sales_30d.rename(columns={"qty": "l30d_sales"}, inplace=True)
    merged = merged.merge(sales_30d[["pharmacy_id", "item_code", "l30d_sales"]], on=["pharmacy_id", "item_code"], how="left")
    merged["l30d_sales"] = merged["l30d_sales"].fillna(0.0)



    merged["effective_qty"] = merged["total_qty"] + merged["in_transit_qty"]
    merged["replenish_mode"] = "max"

    if config_df is not None and not config_df.empty:
        config = config_df.copy()
        config.columns = [c.strip().lower().replace(" ", "_") for c in config.columns]
        for idx, row in config.iterrows():
            cat = str(row.get("category", "")).strip().upper()
            mode_val = str(row.get("tools", "max")).strip().lower()
            applied_to_str = str(row.get("applied_to", "")).strip()

            if pd.isna(row.get("applied_to")) or not applied_to_str or applied_to_str.lower() in ["nan", "none", ""]:
                mask = (merged["abc_xyz"] == cat)
            else:
                pharmacies = [p.strip() for p in applied_to_str.split(",") if p.strip()]
                mask = (merged["abc_xyz"] == cat) & (merged["pharmacy_id"].astype(str).str.strip().isin(pharmacies))

            merged.loc[mask, "replenish_mode"] = mode_val

    plano_allowed = set(plano_df["item_code"].astype(str).str.strip()) if plano_df is not None and not plano_df.empty and "item_code" in plano_df.columns else None

    # Vectorized calculation
    is_plano_disallowed = merged["item_code"].astype(str).str.strip().isin(plano_allowed) == False if plano_allowed else pd.Series(False, index=merged.index)
    is_no_follow = merged["plano_follow"].fillna("").astype(str).str.strip().str.lower() == "no"
    
    target_val = np.where(merged["replenish_mode"].str.lower() == "max", merged["maximum_stock"], merged["minimum_stock"])
    overstock_mask = merged["total_qty"] > target_val
    no_need_min_mask = merged["total_qty"] >= merged["minimum_stock"]
    
    normal_qty = np.maximum(target_val - merged["effective_qty"], 0.0)

    res = np.where(normal_qty <= 0, "No need to replenish", np.round(normal_qty, 2).astype(str))
    res = np.where(no_need_min_mask, "No need to replenish", res)
    res = np.where(overstock_mask, "Overstock", res)
    res = np.where(is_no_follow, "No Follow", res)
    if plano_allowed:
        res = np.where(is_plano_disallowed, "Manually Review", res)

    merged["replenish_qty"] = res

    # Vectorized Hold rules override
    if hold_rules_dict:
        item_codes = merged["item_code"].astype(str).str.strip().values
        pharmacy_ids = merged["pharmacy_id"].astype(str).str.strip().values
        replenish_qtys = merged["replenish_qty"].values
        new_qtys = list(replenish_qtys)
        for i in range(len(item_codes)):
            item = item_codes[i]
            sku_rules = hold_rules_dict.get(item)
            if sku_rules:
                rule = sku_rules.get(pharmacy_ids[i], sku_rules.get("all"))
                if rule:
                    r_lower = str(rule).strip().lower()
                    if r_lower == "hold":
                        new_qtys[i] = "Hold Product"
                    elif r_lower in ["hold to stock out", "hold to stock-out"]:
                        new_qtys[i] = "Hold to Stock Out"
        merged["replenish_qty"] = new_qtys

    return merged

# Stage 6: Ordering & TMP Allocation
def _sync_replenish_qty_to_override(merged, affected_mask):
    """Both planogram overrides work in BOX/ordering units (target_gap, ordered_qty_final), but
    replenish_qty (Stage 5's output) is in SALES units and was never touched by either — caught
    during verification: a row correctly got ordered_qty_final=1 (box) from the planogram floor,
    but the dashboard showed Replenish Qty=0 because it reads replenish_qty directly, which was
    still the stale pre-override "Overstock" string. Keep them consistent for every row either
    override actually changed.
    """
    if not affected_mask.any():
        return
    final_qty = pd.to_numeric(merged["ordered_qty_final"], errors="coerce")
    conv = pd.to_numeric(merged.get("conversion_factor", 1.0), errors="coerce").fillna(1.0)
    numeric_positive = affected_mask & final_qty.notna() & (final_qty > 0)
    merged.loc[numeric_positive, "replenish_qty"] = (final_qty.loc[numeric_positive] * conv.loc[numeric_positive]).astype(str)
    status_only = affected_mask & ~numeric_positive
    merged.loc[status_only, "replenish_qty"] = merged.loc[status_only, "final_status_after_allocation"]

def apply_hold_to_plano_rules(merged, hold_to_plano_rules_dict, plano_unit_lookup):
    """Override the pre-TMP order qty with the raw planogram gap for any SKU+pharmacy matched by
    a hold_to_plano.csv rule (same sku/type/applied_to shape as hold.csv — any matching row
    activates this, regardless of what 'type' says). Deducts current effective stock (converted
    to ordering units) from the planogram's target unit quantity.

    Runs BEFORE TMP allocation (BR-12) — a planogram-driven quantity can still get capped by a
    real warehouse shortage afterward, same as any other demand.

    This was loaded (hold_to_plano_df) but never actually applied anywhere in this script — the
    Control Center's "Hold to Planogram" tab has been fully functional-looking UI with zero effect
    on any calculated number. Ported from replen_v2.py's apply_hold_to_plano_rules(), adapted to
    this script's status strings and to key the planogram lookup by pharmacy_id instead of
    pharmacy_name (more reliable — see build_plano_unit_lookup).
    """
    merged["hold_to_plano_applied"] = False
    if not hold_to_plano_rules_dict:
        return merged

    matched_rules = pd.Series(
        [get_hold_rule(item, pharm, hold_to_plano_rules_dict) for item, pharm in zip(merged["item_code"], merged["pharmacy_id"])],
        index=merged.index, dtype="object",
    )
    matched = matched_rules.notna()
    if not matched.any():
        return merged

    # Don't let a planogram-fill rule reactivate a SKU that's explicitly excluded from ordering
    # for other reasons (planogram-follow=No, or an explicit hold). It's fine to override plain
    # "Overstock"/"No need to replenish" — that's the whole point of this rule.
    current_status = merged["final_status_after_allocation"].fillna("").astype(str).str.strip().str.lower()
    protected = current_status.isin(["no follow", "hold product", "hold to stock out"])
    applicable = matched & ~protected
    if not applicable.any():
        return merged

    plano_qty = pd.Series(
        [plano_unit_lookup.get((str(pid).strip(), str(item).strip())) for pid, item in zip(merged["pharmacy_id"], merged["item_code"])],
        index=merged.index, dtype="float64",
    )
    effective_qty = pd.to_numeric(merged.get("effective_qty", 0.0), errors="coerce").fillna(0.0)
    conversion_factor = pd.to_numeric(merged.get("conversion_factor", np.nan), errors="coerce")

    valid_plano = applicable & plano_qty.notna() & conversion_factor.gt(0)
    missing_plano = applicable & plano_qty.isna()
    missing_conversion = applicable & plano_qty.notna() & ~conversion_factor.gt(0)

    # plano_unit_qty is in ordering units (boxes); effective_qty is in sales units — convert
    # effective stock to ordering units first, then compute the remaining gap.
    order_gap = pd.Series(0.0, index=merged.index)
    order_gap.loc[valid_plano] = np.maximum(
        np.ceil(plano_qty.loc[valid_plano] - effective_qty.loc[valid_plano] / conversion_factor.loc[valid_plano]),
        0.0,
    )

    merged.loc[valid_plano, "ordered_qty_final"] = order_gap.loc[valid_plano]
    merged.loc[valid_plano, "ordered_qty_requested"] = order_gap.loc[valid_plano]
    merged.loc[valid_plano, "hold_to_plano_applied"] = True

    positive_gap = valid_plano & order_gap.gt(0)
    zero_gap = valid_plano & order_gap.le(0)
    merged.loc[positive_gap, "final_status_after_allocation"] = order_gap.loc[positive_gap].astype(str)
    merged.loc[zero_gap, "final_status_after_allocation"] = "No need to replenish"

    # A matched rule must never fall back to the forecast-based number when its planogram
    # quantity or conversion factor is unavailable — force zero and say why, rather than silently
    # ordering the pre-override forecast amount.
    merged.loc[missing_plano, "final_status_after_allocation"] = "Missing planogram quantity"
    merged.loc[missing_conversion, "final_status_after_allocation"] = "Missing conversion factor"
    invalid_input = missing_plano | missing_conversion
    merged.loc[invalid_input, "ordered_qty_final"] = 0.0
    merged.loc[invalid_input, "ordered_qty_requested"] = 0.0
    merged.loc[invalid_input, "hold_to_plano_applied"] = True

    _sync_replenish_qty_to_override(merged, merged["hold_to_plano_applied"])
    return merged

def apply_plano_stock_ratio_gate(merged):
    """A third gate stacked on top of the two planogram mechanisms above, run right after both
    (so it sees their final ordered_qty_final) and still before TMP allocation.

    Even when the planogram floor or the hold_to_plano allowlist says a SKU should replenish,
    skip it anyway if the pharmacy already holds a healthy fraction of the planogram's own
    target — only let a planogram-driven order actually proceed into TMP allocation when current
    stock covers LESS than 60% of the planogram target (stock/plano_qty ratio < 0.6). At or above
    that ratio the shelf isn't meaningfully under-stocked relative to its own target, so this
    forces the qty to 0 instead of letting it compete for (possibly scarce) central-warehouse
    stock over a SKU that's already reasonably covered.

    Only touches rows either planogram mechanism actually flagged (plano_unit_gap_applied OR
    hold_to_plano_applied) — a row neither mechanism touched is untouched by this gate too.
    """
    plano_qty = pd.to_numeric(merged.get("plano_unit_qty"), errors="coerce")
    conversion_factor = pd.to_numeric(merged.get("conversion_factor", 1.0), errors="coerce").fillna(1.0)
    effective_qty = pd.to_numeric(merged.get("effective_qty", 0.0), errors="coerce").fillna(0.0)

    # plano_qty is in ordering units (boxes); convert to sales units so it's directly comparable
    # to effective_qty (sales units) — same unit-matching rule as the two mechanisms above.
    plano_qty_sales_units = plano_qty * conversion_factor
    ratio = np.where(plano_qty_sales_units > 0, effective_qty / plano_qty_sales_units, np.nan)
    merged["plano_stock_ratio"] = ratio

    plano_touched = merged.get("plano_unit_gap_applied", False).fillna(False) | merged.get("hold_to_plano_applied", False).fillna(False)
    suppress = plano_touched & pd.notna(ratio) & (ratio >= 0.6)

    merged.loc[suppress, "ordered_qty_final"] = 0.0
    merged.loc[suppress, "ordered_qty_requested"] = 0.0
    merged.loc[suppress, "final_status_after_allocation"] = "No need to replenish"

    _sync_replenish_qty_to_override(merged, suppress)
    return merged

def apply_plano_unit_gap_floor(merged, plano_unit_lookup):
    """Unconditional planogram floor — separate from apply_hold_to_plano_rules() above, and easy
    to conflate with it (both key off plano.csv). This one applies to ANY SKU present in
    plano.csv at all, not just the ones explicitly listed in hold_to_plano.csv, and it only ever
    bumps the qty UP (a floor), never replaces/decreases it: if the planogram target implies more
    units are needed than what's already about to be ordered, order up to the target instead.
    A SKU with no hold_to_plano.csv rule (like ID100529-1, caught during verification — the demand
    formula alone said "Overstock" even though plano.csv wants 1 more box) still gets this floor
    applied. Ported from the inline 'plano_unit_gap_applied' block inside replen_v2.py's
    run_inventory_ordering(), which real-world testing confirmed does something meaningfully
    different from apply_hold_to_plano_rules() despite both touching the same source file.
    """
    merged["target_gap"] = np.nan
    merged["plano_unit_gap_applied"] = False

    plano_qty = pd.Series(
        [plano_unit_lookup.get((str(pid).strip(), str(item).strip())) for pid, item in zip(merged["pharmacy_id"], merged["item_code"])],
        index=merged.index, dtype="float64",
    )
    # Persisted (not just a local variable) so it survives into ordering_df.csv / the dashboard —
    # both the "Plano QTY" display column and apply_plano_stock_ratio_gate() below need the raw
    # box-unit target, not just this function's derived target_gap/plano_unit_gap_applied.
    merged["plano_unit_qty"] = plano_qty
    conversion_factor = pd.to_numeric(merged.get("conversion_factor", np.nan), errors="coerce")
    effective_qty = pd.to_numeric(merged.get("effective_qty", 0.0), errors="coerce").fillna(0.0)

    valid_gap = plano_qty.notna() & conversion_factor.gt(0)
    if valid_gap.any():
        # plano_unit_qty is in ordering units (boxes); effective_qty is in sales units — convert
        # effective stock to ordering units first, then compute the remaining gap.
        gaps = np.ceil(plano_qty.loc[valid_gap] - effective_qty.loc[valid_gap] / conversion_factor.loc[valid_gap])
        merged.loc[valid_gap, "target_gap"] = np.maximum(gaps, 0.0)

        current_qty = pd.to_numeric(merged["ordered_qty_final"], errors="coerce").fillna(0.0)
        current_status = merged["final_status_after_allocation"].fillna("").astype(str).str.strip().str.lower()
        # replen_v2.py's reference only guards against "stockout tmp" here (which can't actually
        # occur yet at this point in the pipeline — TMP allocation hasn't run). Also guarding
        # no-follow/hold statuses here, matching apply_hold_to_plano_rules()'s protection, so this
        # unconditional floor can't quietly reactivate a SKU someone explicitly suppressed.
        protected = current_status.isin(["stockout tmp", "no follow", "hold product", "hold to stock out"])
        gap_applies = merged["target_gap"].gt(current_qty) & ~protected

        merged.loc[gap_applies, "ordered_qty_final"] = merged.loc[gap_applies, "target_gap"]
        merged.loc[gap_applies, "ordered_qty_requested"] = merged.loc[gap_applies, "target_gap"]
        merged.loc[gap_applies, "final_status_after_allocation"] = merged.loc[gap_applies, "target_gap"].astype(str)
        merged["plano_unit_gap_applied"] = gap_applies

    # A planogram target of exactly 0 means this SKU shouldn't be stocked at this pharmacy at all.
    plano_zero = plano_qty == 0
    merged.loc[plano_zero, "ordered_qty_final"] = 0.0
    merged.loc[plano_zero, "ordered_qty_requested"] = 0.0
    merged.loc[plano_zero, "final_status_after_allocation"] = "No need to replenish"

    _sync_replenish_qty_to_override(merged, merged["plano_unit_gap_applied"] | plano_zero)
    return merged

def append_missing_plano_items(merged, plano_df, stock_transit_df, conversion_df, price_df):
    """A SKU+pharmacy with a real plano.csv target but ZERO sales history never gets a row at ALL
    in this pipeline — every stage from ABC analysis onward is built by grouping sales_df, so a
    never-sold SKU on the planogram is completely invisible no matter how short its stock is
    against that target (caught during verification: ID127973-1 @ 14038 — 0 sales rows, plano
    wants 2 units, effective stock 0, and the row simply didn't exist anywhere in the output).

    Ported from replen_v2.py's append_missing_plano_items(): synthesizes a row for every
    plano.csv (pharmacy_id, item_code) key missing from `merged`, computing its target gap
    directly from stock_transit/conversion/price data (TMP status/qty is deliberately left for
    the single downstream merge in run_inventory_ordering to fill in for all rows uniformly).
    Never touches sales-driven fields (abc_xyz, forecast, l7d/l30d sales) since there's no sales
    history to derive them from — left as zero/blank rather than fabricated.

    Must run BEFORE the TMP merge/allocation below (not as a separate later pass) so these new
    rows actually get pooled with existing rows for the same SKU during allocation, instead of
    silently bypassing the TMP stock constraint entirely.
    """
    if plano_df is None or plano_df.empty or not {"item_code", "unit_qty", "pharmacy_id"}.issubset(plano_df.columns):
        return merged

    plano_items = plano_df.copy()
    plano_items["item_code"] = plano_items["item_code"].astype(str).str.strip()
    plano_items["unit_qty"] = pd.to_numeric(plano_items["unit_qty"], errors="coerce")
    # Same float-pharmacy_id-becomes-"14001.0" issue as build_plano_unit_lookup — fix identically.
    plano_items["pharmacy_id"] = pd.to_numeric(plano_items["pharmacy_id"], errors="coerce")
    plano_items = plano_items.dropna(subset=["item_code", "unit_qty", "pharmacy_id"])
    plano_items["pharmacy_id"] = plano_items["pharmacy_id"].astype("int64").astype(str)

    key_cols = ["pharmacy_id", "item_code"]
    merged["pharmacy_id"] = merged["pharmacy_id"].astype(str).str.strip()
    existing_keys = pd.MultiIndex.from_frame(merged[key_cols].drop_duplicates())
    plano_keys = pd.MultiIndex.from_frame(plano_items[key_cols])
    missing_items = plano_items.loc[~plano_keys.isin(existing_keys)].drop_duplicates(key_cols, keep="last")
    if missing_items.empty:
        return merged

    cols_to_carry = [c for c in ["pharmacy_id", "item_code", "pharmacy_name"] if c in missing_items.columns]
    added = missing_items[cols_to_carry].copy()
    added["plano_unit_qty"] = missing_items["unit_qty"].values
    if "pharmacy_name" not in added.columns:
        added["pharmacy_name"] = "PHB " + added["pharmacy_id"]

    # These rows never went through Stage 5's balancing, so effective_qty has to be computed
    # directly from stock_transit_df here instead of being already present.
    stock_df = load_stock_and_intransit_combined_df(stock_transit_df)
    if not stock_df.empty and "pharmacy_id" in stock_df.columns:
        stock_df = stock_df.copy()
        stock_df["pharmacy_id"] = stock_df["pharmacy_id"].astype(str).str.strip()
        stock_df["item_code"] = stock_df["item_code"].astype(str).str.strip()
        added = added.merge(stock_df[key_cols + ["total_qty", "in_transit_qty"]], on=key_cols, how="left")
    added["total_qty"] = pd.to_numeric(added.get("total_qty"), errors="coerce").fillna(0.0)
    added["in_transit_qty"] = pd.to_numeric(added.get("in_transit_qty"), errors="coerce").fillna(0.0)
    added["effective_qty"] = added["total_qty"] + added["in_transit_qty"]

    conv_map = {}
    if conversion_df is not None and not conversion_df.empty:
        c_clean = conversion_df.copy()
        c_clean.columns = [c.strip().lower().replace(" ", "_") for c in c_clean.columns]
        f_col = next((c for c in c_clean.columns if "conversion" in c or "factor" in c), "conversion_factor")
        sku_col = next((c for c in c_clean.columns if "item_code" in c or "sku" in c), "item_code")
        c_clean[sku_col] = c_clean[sku_col].astype(str).str.strip()
        c_clean[f_col] = pd.to_numeric(c_clean[f_col], errors="coerce").fillna(1.0)
        conv_map = c_clean.groupby(sku_col)[f_col].last().to_dict()
    added["conversion_factor"] = added["item_code"].map(conv_map).fillna(1.0).replace(0, 1.0)

    # Same target-gap math as the floor above — from scratch, since there's no prior demand-based
    # qty for it to beat.
    valid = added["plano_unit_qty"].notna() & added["conversion_factor"].gt(0)
    added["target_gap"] = 0.0
    added.loc[valid, "target_gap"] = np.maximum(
        np.ceil(added.loc[valid, "plano_unit_qty"] - added.loc[valid, "effective_qty"] / added.loc[valid, "conversion_factor"]),
        0.0,
    )
    added["ordered_qty_requested"] = added["target_gap"]
    added["ordered_qty_final"] = added["target_gap"]
    added["plano_unit_gap_applied"] = added["target_gap"].gt(0)
    added["hold_to_plano_applied"] = False
    added["replenish_qty"] = np.where(added["target_gap"].gt(0), added["target_gap"].astype(str), "No need to replenish")
    added["final_status_after_allocation"] = added["replenish_qty"]
    added["missing_from_sales"] = True
    # TMP stock merge + discontinued handling deliberately NOT done here — run_inventory_ordering
    # does that single merge on the full merged set (old + these new rows together) right after
    # this call returns. Doing it here too would collide column names (tmp_qty_x/tmp_qty_y) on
    # the second merge.

    plano_zero = added["plano_unit_qty"] == 0
    added.loc[plano_zero, "ordered_qty_final"] = 0.0
    added.loc[plano_zero, "ordered_qty_requested"] = 0.0
    added.loc[plano_zero, "final_status_after_allocation"] = "No need to replenish"
    added.loc[plano_zero, "plano_unit_gap_applied"] = False

    price_map = {}
    if price_df is not None and not price_df.empty:
        p_clean = price_df.copy()
        p_clean.columns = [c.strip().lower().replace(" ", "_") for c in p_clean.columns]
        p_col = next((c for c in p_clean.columns if "price" in c or "harga" in c), "price")
        sku_col = next((c for c in p_clean.columns if "item_code" in c or "sku" in c), "item_code")
        p_clean[sku_col] = p_clean[sku_col].astype(str).str.strip()
        p_clean[p_col] = pd.to_numeric(p_clean[p_col], errors="coerce").fillna(0.0)
        price_map = p_clean.groupby(sku_col)[p_col].last().to_dict()
    added["price"] = added["item_code"].map(price_map).fillna(0.0)
    added["total_order_val"] = added["ordered_qty_final"] * added["price"]

    # No sales history exists to derive these from — leave at zero rather than fabricating them.
    added["l7d_sales"] = 0.0
    added["l30d_sales"] = 0.0

    return pd.concat([merged, added], ignore_index=True, sort=False)

def run_inventory_ordering(balancing_df, abc_xyz_df, target_df, conversion_df, stock_tmp_df, price_df, plano_df=None, hold_to_plano_rules_dict=None, stock_transit_df=None):
    print("[6/6] Running Inventory Ordering & TMP Allocation...")
    merged = balancing_df.copy()

    # Price merge
    if price_df is not None and not price_df.empty:
        p_clean = price_df.copy()
        p_clean.columns = [c.strip().lower().replace(" ", "_") for c in p_clean.columns]
        p_col = next((c for c in p_clean.columns if "price" in c or "harga" in c), "price")
        sku_col = next((c for c in p_clean.columns if "item_code" in c or "sku" in c), "item_code")
        p_clean[sku_col] = p_clean[sku_col].astype(str).str.strip()
        p_clean[p_col] = pd.to_numeric(p_clean[p_col], errors="coerce").fillna(0.0)
        price_map = p_clean.groupby(sku_col)[p_col].last().to_dict()
        merged["price"] = merged["item_code"].astype(str).str.strip().map(price_map).fillna(0.0)
    else:
        merged["price"] = 0.0

    # Conversion factor merge
    if conversion_df is not None and not conversion_df.empty:
        c_clean = conversion_df.copy()
        c_clean.columns = [c.strip().lower().replace(" ", "_") for c in c_clean.columns]
        f_col = next((c for c in c_clean.columns if "conversion" in c or "factor" in c), "conversion_factor")
        sku_col = next((c for c in c_clean.columns if "item_code" in c or "sku" in c), "item_code")
        c_clean[sku_col] = c_clean[sku_col].astype(str).str.strip()
        c_clean[f_col] = pd.to_numeric(c_clean[f_col], errors="coerce").fillna(1.0)
        conv_map = c_clean.groupby(sku_col)[f_col].last().to_dict()
        merged["conversion_factor"] = merged["item_code"].astype(str).str.strip().map(conv_map).fillna(1.0)
    else:
        merged["conversion_factor"] = 1.0

    merged["conversion_factor"] = merged["conversion_factor"].replace(0, 1.0)

    # Pre-TMP requested qty (ordering/box units) — the "ideal" quantity the formula wants, kept as
    # its own persisted column instead of being silently overwritten by allocation below. This closes
    # the audit-trail gap: previously nothing recorded what a pharmacy asked for before TMP capped it.
    replen_num = pd.to_numeric(merged["replenish_qty"], errors="coerce")
    valid_mask = replen_num.notna() & (replen_num > 0)
    requested_qty = np.zeros(len(merged), dtype=float)
    if valid_mask.any():
        requested_qty[valid_mask] = np.ceil(replen_num[valid_mask] / merged.loc[valid_mask, "conversion_factor"])
    merged["ordered_qty_requested"] = requested_qty
    merged["ordered_qty_final"] = requested_qty.copy()
    merged["final_status_after_allocation"] = merged["replenish_qty"]

    # Two separate, stackable planogram mechanisms — both must run before TMP allocation (BR-12):
    # 1. The unconditional floor (applies to any SKU in plano.csv at all, only ever raises the qty)
    # 2. The explicit hold_to_plano.csv allowlist override (fully replaces the qty for listed SKUs)
    # Order matches replen_v2.py's reference: floor first, then the explicit override, which — for
    # the ~32 SKUs it applies to — fully overwrites whatever the floor just set anyway.
    merged["pharmacy_id"] = merged["pharmacy_id"].astype(str).str.strip()
    plano_unit_lookup = build_plano_unit_lookup(plano_df)
    merged = apply_plano_unit_gap_floor(merged, plano_unit_lookup)

    # SKUs with a real plano.csv target but zero sales history don't have a row to override yet —
    # add them now, BEFORE hold_to_plano/TMP run, so they're eligible for both (in particular, so
    # they get correctly pooled with existing rows for the same SKU during TMP allocation below,
    # instead of silently bypassing the stock constraint by arriving after allocation already ran).
    merged = append_missing_plano_items(merged, plano_df, stock_transit_df, conversion_df, price_df)

    merged = apply_hold_to_plano_rules(merged, hold_to_plano_rules_dict, plano_unit_lookup)

    # Third gate, still before TMP allocation: a row either planogram mechanism just touched only
    # actually proceeds if current stock covers under 60% of the planogram's own target — see
    # apply_plano_stock_ratio_gate()'s docstring.
    merged = apply_plano_stock_ratio_gate(merged)

    # TMP stock merge (tmp_stock.csv columns: Status, sku_code, quantity)
    merged["item_code"] = merged["item_code"].astype(str).str.strip()
    tmp_df = load_stock_tmp_df(stock_tmp_df)
    if not tmp_df.empty:
        merged = merged.merge(tmp_df, on="item_code", how="left")

    # Discontinued at TMP -> forced to zero regardless of demand, before allocation runs
    if "tmp_status" in merged.columns:
        discontinued = merged["tmp_status"].fillna("") == "discontinue"
        merged.loc[discontinued, "ordered_qty_final"] = 0.0
        merged.loc[discontinued, "final_status_after_allocation"] = "Discontinue"

    # Proportional (pro-rata) TMP allocation per SKU, across every pharmacy asking for it.
    # available < total demand -> floor each pharmacy's proportional share, then hand out the
    # leftover whole units one at a time to the largest fractional remainder first, tied-broken
    # by lowest pharmacy_id. Mirrors replen_v2.py's allocate_tmp_stock() (the Jupyter reference).
    def _allocate_tmp(group):
        if "tmp_qty" not in group.columns:
            return group
        available = pd.to_numeric(pd.Series([group["tmp_qty"].iloc[0]]), errors="coerce").iloc[0]
        total_need = group["ordered_qty_final"].sum()

        if pd.isna(available):
            return group  # SKU not tracked in tmp_stock.csv -> can't check, leave as requested
        if available <= 0:
            had_demand = group["ordered_qty_final"] > 0
            group.loc[had_demand, "ordered_qty_final"] = 0.0
            group.loc[had_demand, "final_status_after_allocation"] = "Stockout TMP"
            return group
        if total_need <= 0 or available >= total_need:
            return group  # nothing to ration, or nobody actually wants any

        shares = group["ordered_qty_final"] * (available / total_need)
        floored = np.floor(shares).astype(int)
        remaining = int(available - floored.sum())
        if remaining > 0:
            remainders = shares - floored
            pharm_ids = pd.to_numeric(group["pharmacy_id"], errors="coerce").fillna(999999)
            order = pd.DataFrame(
                {"remainder": remainders, "pharmacy_id": pharm_ids}, index=group.index
            ).sort_values(["remainder", "pharmacy_id"], ascending=[False, True]).index
            for idx in order:
                if remaining <= 0:
                    break
                if floored.loc[idx] < group.loc[idx, "ordered_qty_final"]:
                    floored.loc[idx] += 1
                    remaining -= 1

        capped = floored.astype(float) < group["ordered_qty_final"]
        group["ordered_qty_final"] = floored.astype(float)
        group.loc[capped, "final_status_after_allocation"] = "Capped by TMP Allocation"
        return group

    merged = pd.concat(
        [_allocate_tmp(g.copy()) for _, g in merged.groupby("item_code", observed=True)],
        ignore_index=True,
    )
    merged["tmp_available_qty"] = merged.get("tmp_qty")
    merged["tmp_item_status"] = merged.get("tmp_status")

    # final_status_after_allocation only gets updated by the TMP step above in two cases (stockout
    # / actual capping) — every other row keeps whatever numeric string was written to it several
    # stages earlier, which could be in SALES units (Stage 5's original value, if nothing ever
    # touched it again) or BOX units (if the planogram floor/hold-to-plano touched it), depending
    # entirely on which override mechanisms happened to run for that specific row — caught when a
    # user noticed two structurally-identical rows (untouched, no TMP capping) showing wildly
    # different "Status" numbers relative to their "Ordered" qty, with no way to tell which unit
    # a given row's number was in. Force every numeric case to match ordered_qty_final's units
    # (box) — leave any non-numeric status label (Overstock, Capped by TMP Allocation, etc.) as-is.
    numeric_status = pd.to_numeric(merged["final_status_after_allocation"], errors="coerce")
    is_numeric_status = numeric_status.notna()
    merged.loc[is_numeric_status, "final_status_after_allocation"] = merged.loc[is_numeric_status, "ordered_qty_final"].astype(str)

    merged["final_status"] = merged["final_status_after_allocation"]

    # Same replenish_qty/ordered_qty_final consistency issue as the planogram overrides above,
    # but for TMP allocation's own capping — pre-existing, not introduced today, just caught by
    # the same investigation: a row correctly gets ordered_qty_final=0 (or partially reduced) when
    # TMP allocation caps it, but replenish_qty (Stage 5's sales-unit output) was never touched, so
    # it kept showing the stale pre-cap demand. The dashboard reads replenish_qty directly, so a
    # fully-stocked-out SKU could still display a real-looking positive "Replenish Qty".
    tmp_affected = merged["final_status_after_allocation"].isin(["Capped by TMP Allocation", "Stockout TMP", "Discontinue"])
    _sync_replenish_qty_to_override(merged, tmp_affected)

    # Total Position / L7D / L30D flags now reflect the ACTUAL final allocated qty, not the
    # pre-allocation ask — otherwise a SKU capped by TMP shortage would still show flag ratios
    # computed against a quantity the pharmacy never actually receives.
    #
    # UNIT BUG (caught by the user): ordered_qty_final is in BOX/ordering units (it went through
    # ceil(replenish_qty / conversion_factor)), but total_qty/in_transit_qty/l7d_sales/l30d_sales
    # are all in SALES units. Adding them directly mixed units — e.g. conversion_factor=100,
    # replenish_qty=72 sales units -> ordered_qty_final=1 box -> total_position was computing
    # "1 box + 0 stock" = 1, instead of the correct 72 sales units. Convert the allocated box qty
    # back to sales units first so everything stays in the same unit as the sales figures.
    final_qty_sales_units = merged["ordered_qty_final"] * merged["conversion_factor"]
    eff_stock = pd.to_numeric(merged.get("total_qty", 0), errors="coerce").fillna(0.0) + pd.to_numeric(merged.get("in_transit_qty", 0), errors="coerce").fillna(0.0)
    total_pos = final_qty_sales_units + eff_stock
    merged["total_position"] = total_pos

    l7d_val = pd.to_numeric(merged.get("l7d_sales", 0), errors="coerce").fillna(0.0)
    l30d_val = pd.to_numeric(merged.get("l30d_sales", 0), errors="coerce").fillna(0.0)

    merged["l7d_ratio"] = np.where(l7d_val > 0, (total_pos / l7d_val).round(2), np.nan)
    merged["l30d_ratio"] = np.where(l30d_val > 0, (total_pos / l30d_val).round(2), np.nan)
    # ">30%" means position exceeds L7D sales BY 30% (ratio > 1.30), not merely >30% of it.
    # The old ">0.30" raw-ratio check flagged almost every order (30% of a week is ~2 days of
    # cover — trivial to exceed) — same fix already applied on the dashboard side.
    merged["l7d_flag"] = (l7d_val > 0) & (merged["l7d_ratio"] > 1.30)
    merged["l30d_flag"] = (l30d_val > 0) & (merged["l30d_ratio"] > 1.00)

    merged["total_order_val"] = merged["ordered_qty_final"] * merged["price"]
    return merged

def load_stock_tmp_df(stock_tmp_df):
    """Normalize tmp_stock.csv (columns: Status, sku_code, quantity) to item_code/tmp_qty/tmp_status."""
    if stock_tmp_df is None or stock_tmp_df.empty:
        return pd.DataFrame(columns=["item_code", "tmp_qty", "tmp_status"])
    df = stock_tmp_df.copy()
    cols = {c.strip().lower().replace(" ", "_"): c for c in df.columns}
    item_col = next((cols[c] for c in ["sku_code", "item_code", "sku", "code"] if c in cols), None)
    qty_col = next((cols[c] for c in ["quantity", "tmp_qty", "qty", "available_qty"] if c in cols), None)
    status_col = next((cols[c] for c in ["status", "item_status", "tmp_status"] if c in cols), None)
    if not item_col or not qty_col:
        return pd.DataFrame(columns=["item_code", "tmp_qty", "tmp_status"])
    rename = {item_col: "item_code", qty_col: "tmp_qty"}
    if status_col:
        rename[status_col] = "tmp_status"
    df = df.rename(columns=rename)
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["tmp_qty"] = pd.to_numeric(df["tmp_qty"], errors="coerce")
    keep = ["item_code", "tmp_qty"]
    if "tmp_status" in df.columns:
        df["tmp_status"] = df["tmp_status"].fillna("").astype(str).str.strip().str.lower()
        keep.append("tmp_status")
    return df.dropna(subset=["item_code"]).drop_duplicates("item_code", keep="last")[keep]




def integrate_additional_external_orders(ordering_df, additional_df, price_df):
    """Additional Orders (Non-SCM) — manually-specified pharmacy+SKU+qty entries that are added
    ON TOP of the calculated result, added AFTER TMP allocation (they're sourced through a
    different channel entirely — "Non-SCM" — so they deliberately bypass the TMP stock
    constraint rather than competing for it). Ported from replen_v2.py's
    integrate_additional_external_orders(): tops up ordered_qty_final for a SKU+pharmacy that
    already has a row, or creates a brand-new row if it doesn't.

    Was loaded nowhere and read nowhere before today — the Control Center "Additional Orders" tab
    had no CSV backing at all.
    """
    ordering_df["is_additional_external"] = ordering_df.get("is_additional_external", "No")
    ordering_df["is_additional_external"] = ordering_df["is_additional_external"].fillna("No")
    if additional_df is None or additional_df.empty:
        return ordering_df

    df = additional_df.copy()
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    if not {"pharmacy_id", "item_code", "qty"}.issubset(df.columns):
        print("Warning: additional_orders.csv missing pharmacy_id/item_code/qty — skipping integration.")
        return ordering_df

    price_map = {}
    if price_df is not None and not price_df.empty and {"item_code", "price"}.issubset(price_df.columns):
        p_clean = price_df.copy()
        p_clean["item_code"] = p_clean["item_code"].astype(str).str.strip()
        price_map = p_clean.drop_duplicates("item_code", keep="last").set_index("item_code")["price"].to_dict()

    pharmacy_map = ordering_df.dropna(subset=["pharmacy_id"]).drop_duplicates("pharmacy_id").set_index("pharmacy_id")["pharmacy_name"].to_dict()

    result = ordering_df.copy()
    result["pharmacy_id"] = result["pharmacy_id"].astype(str).str.strip()
    result["item_code"] = result["item_code"].astype(str).str.strip()
    index_map = result.groupby(["pharmacy_id", "item_code"]).indices

    new_rows = []
    for _, row in df.iterrows():
        pid = str(row.get("pharmacy_id", "")).strip()
        item = str(row.get("item_code", "")).strip()
        try:
            add_qty = float(row.get("qty", 0))
        except (ValueError, TypeError):
            add_qty = 0.0
        if add_qty <= 0 or not pid or not item:
            continue

        key = (pid, item)
        if key in index_map:
            idx = index_map[key][0]
            curr = pd.to_numeric(result.loc[idx, "ordered_qty_final"], errors="coerce")
            curr = 0.0 if pd.isna(curr) else curr
            new_qty = curr + add_qty
            result.loc[idx, "ordered_qty_final"] = new_qty
            curr_requested = pd.to_numeric(result.loc[idx, "ordered_qty_requested"], errors="coerce")
            curr_requested = 0.0 if pd.isna(curr_requested) else curr_requested
            result.loc[idx, "ordered_qty_requested"] = curr_requested + add_qty
            result.loc[idx, "final_status_after_allocation"] = str(new_qty)
            result.loc[idx, "final_status"] = str(new_qty)
            result.loc[idx, "is_additional_external"] = "Yes"
            conv = pd.to_numeric(result.loc[idx, "conversion_factor"], errors="coerce")
            conv = 1.0 if pd.isna(conv) or conv <= 0 else conv
            result.loc[idx, "replenish_qty"] = str(new_qty * conv)
            result.loc[idx, "price"] = result.loc[idx, "price"] if pd.notna(result.loc[idx, "price"]) else price_map.get(item, 0.0)
            result.loc[idx, "total_order_val"] = new_qty * pd.to_numeric(result.loc[idx, "price"], errors="coerce")
        else:
            item_price = float(price_map.get(item, 0.0))
            new_rows.append({
                "pharmacy_id": pid,
                "pharmacy_name": pharmacy_map.get(pid, "Unknown Pharmacy"),
                "item_code": item,
                "ordered_qty_final": add_qty,
                "ordered_qty_requested": add_qty,
                "replenish_qty": str(add_qty),
                "final_status": str(add_qty),
                "final_status_after_allocation": str(add_qty),
                "is_additional_external": "Yes",
                "conversion_factor": 1.0,
                "price": item_price,
                "total_order_val": add_qty * item_price,
            })

    if new_rows:
        result = pd.concat([result, pd.DataFrame(new_rows)], ignore_index=True, sort=False)
    return result

def main():
    start_total = time.perf_counter()
    data_dir = "./data"
    print(f"=== Starting Local replenishment execution from CSVs in '{data_dir}' ===")

    ingestion_start = time.perf_counter()
    sales_df = pd.read_csv(os.path.join(data_dir, "sales.csv"))
    plano_df = pd.read_csv(os.path.join(data_dir, "plano.csv"))
    stock_transit_df = pd.read_csv(os.path.join(data_dir, "stock_transit.csv"))
    conversion_df = pd.read_csv(os.path.join(data_dir, "conversion.csv"))
    stock_tmp_df = pd.read_csv(os.path.join(data_dir, "tmp_stock.csv"))
    price_df = pd.read_csv(os.path.join(data_dir, "pricelist.csv"))
    store_df = pd.read_csv(os.path.join(data_dir, "store.csv"))
    
    config_path = os.path.join(data_dir, "config.csv")
    config_df = pd.read_csv(config_path) if os.path.exists(config_path) else pd.DataFrame()
    
    hold_path = os.path.join(data_dir, "hold.csv")
    hold_df = pd.read_csv(hold_path) if os.path.exists(hold_path) else pd.DataFrame()
    
    hold_to_plano_path = os.path.join(data_dir, "hold_to_plano.csv")
    hold_to_plano_df = pd.read_csv(hold_to_plano_path) if os.path.exists(hold_to_plano_path) else pd.DataFrame()

    lt_override_path = os.path.join(data_dir, "lt_override.csv")
    lt_override_df = pd.read_csv(lt_override_path) if os.path.exists(lt_override_path) else pd.DataFrame()
    
    sales_df["pharmacy_id"] = sales_df["pharmacy_id"].astype(str).str.strip()
    sales_df["item_code"] = sales_df["item_code"].astype(str).str.strip()
    if plano_df is not None and not plano_df.empty and "pharmacy_id" in plano_df.columns:
        # plano.csv has some blank pharmacy_id rows, forcing the whole column to float64 (NaN
        # needs it) — a plain .astype(str) here produced "14006.0" instead of "14006", silently
        # breaking every later comparison against clean string IDs in this function, INCLUDING the
        # inactive-pharmacy exclusion filter below (found 2026-08-17: pharmacy 14006, correctly
        # marked inactive in store.csv, still had 2,030 rows in the output — all of them injected
        # by append_missing_plano_items() because this exclusion never actually matched). Same fix
        # as build_plano_unit_lookup(): coerce through a nullable Int64 before stringifying.
        numeric_pid = pd.to_numeric(plano_df["pharmacy_id"], errors="coerce").astype("Int64")
        plano_df["pharmacy_id"] = numeric_pid.astype(str).where(numeric_pid.notna(), "")
        plano_df["item_code"] = plano_df["item_code"].astype(str).str.strip()
    if stock_transit_df is not None and not stock_transit_df.empty and "pharmacy_id" in stock_transit_df.columns:
        # Same NaN-forces-float64 issue as plano_df above — fix at the source so every downstream
        # consumer of this column sees a clean "14001", not "14001.0".
        numeric_pid = pd.to_numeric(stock_transit_df["pharmacy_id"], errors="coerce").astype("Int64")
        stock_transit_df["pharmacy_id"] = numeric_pid.astype(str).where(numeric_pid.notna(), "")
        stock_transit_df["item_code"] = stock_transit_df["item_code"].astype(str).str.strip()

    # Exclude pharmacies store.csv marks as not replenished (replenishment_trigger == "No") BEFORE
    # any stage runs — this used to be entirely ignored: store.csv was loaded only for its name
    # lookup, so an "inactive" store like Cilincing still had full replenishment calculated for it.
    # A pharmacy missing from store.csv entirely is treated as active (permissive default) rather
    # than silently dropped.
    trigger_col = next((c for c in ["replenishment_trigger", "Replenishment Trigger"] if c in store_df.columns), None)
    if trigger_col:
        store_df["pharmacy_id"] = store_df["pharmacy_id"].astype(str).str.strip()
        inactive_ids = set(
            store_df.loc[store_df[trigger_col].astype(str).str.strip().str.lower() == "no", "pharmacy_id"]
        )
        if inactive_ids:
            before_rows = len(sales_df)
            sales_df = sales_df[~sales_df["pharmacy_id"].isin(inactive_ids)]
            if "pharmacy_id" in stock_transit_df.columns:
                # Currently inert either way (run_inventory_balancing left-joins FROM target_df,
                # which already excludes inactive pharmacies via sales_df, so a stray stock_transit
                # row for an inactive pharmacy never matches anything) — fixed anyway for
                # correctness/consistency with the identical bug found in load_stock_and_intransit_
                # combined_df() and plano_df's exclusion filter (same file, same NaN-forces-float64
                # cause: stock_transit.csv's pharmacy_id column).
                stock_pid_numeric = pd.to_numeric(stock_transit_df["pharmacy_id"], errors="coerce").astype("Int64")
                stock_pid_clean = stock_pid_numeric.astype(str).where(stock_pid_numeric.notna(), "")
                stock_transit_df = stock_transit_df[~stock_pid_clean.isin(inactive_ids)]
            if "pharmacy_id" in plano_df.columns:
                plano_df = plano_df[~plano_df["pharmacy_id"].astype(str).str.strip().isin(inactive_ids)]
            print(f"Excluded {len(inactive_ids)} inactive pharmacy(ies) ({', '.join(sorted(inactive_ids))}) — dropped {before_rows - len(sales_df)} sales rows")

    print(f"⏱️ Local CSV ingestion time: {time.perf_counter() - ingestion_start:.4f}s")


    # Run Pipeline Stages
    hold_rules_dict = run_timed("preprocess_hold_rules", preprocess_hold_rules, hold_df)
    hold_to_plano_rules_dict = run_timed("preprocess_hold_to_plano_rules", preprocess_hold_rules, hold_to_plano_df)
    abc_results = run_timed("run_abc_analysis", run_abc_analysis, sales_df)
    abc_xyz = run_timed("run_abc_xyz_analysis", run_abc_xyz_analysis, sales_df, abc_results)
    forecast_df = run_timed("run_forecast_analysis", run_forecast_analysis, sales_df, abc_xyz)
    target_df = run_timed("run_inventory_targets_analysis", run_inventory_targets_analysis, sales_df, forecast_df, config_df, plano_df, lt_override_df)
    balancing_df = run_timed("run_inventory_balancing", run_inventory_balancing, target_df, sales_df, stock_transit_df, config_df, hold_rules_dict, plano_df)
    ordering_df = run_timed("run_inventory_ordering", run_inventory_ordering, balancing_df, abc_xyz, target_df, conversion_df, stock_tmp_df, price_df, plano_df, hold_to_plano_rules_dict, stock_transit_df)

    # Populate pharmacy_name mapping
    pharm_map = store_df.drop_duplicates("pharmacy_id").set_index("pharmacy_id")["pharmacy_name"].to_dict()
    if "pharmacy_name" in sales_df.columns:
        pharm_map.update(sales_df.drop_duplicates("pharmacy_id").set_index("pharmacy_id")["pharmacy_name"].to_dict())

    ordering_df["pharmacy_id"] = ordering_df["pharmacy_id"].astype(str).str.strip()
    ordering_df["pharmacy_name"] = ordering_df["pharmacy_id"].map(pharm_map).fillna("Unknown Pharmacy")

    additional_path = os.path.join(data_dir, "additional_orders.csv")
    additional_df = pd.read_csv(additional_path) if os.path.exists(additional_path) else pd.DataFrame()
    ordering_df = run_timed("integrate_additional_external_orders", integrate_additional_external_orders, ordering_df, additional_df, price_df)

    # Save outputs
    out_ordering = os.path.join(data_dir, "ordering_df.csv")
    ordering_df.to_csv(out_ordering, index=False)
    print(f"Saved result: {out_ordering} ({len(ordering_df)} rows)")


    out_js = os.path.join(data_dir, "ordering_data.js")
    import json
    records = ordering_df.to_dict(orient="records")
    with open(out_js, "w", encoding="utf-8") as f:
        f.write("window.ORDERING_DATA = " + json.dumps(records) + ";")
    print(f"Saved JS bundle: {out_js}")

    total_time = time.perf_counter() - start_total
    print(f"\n🚀 LOCAL EXECUTION COMPLETED SUCCESSFULLY IN: {total_time:.4f} seconds!")


if __name__ == "__main__":
    main()
