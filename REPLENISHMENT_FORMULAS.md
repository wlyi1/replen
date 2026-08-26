# Inofarma Replenishment Engine — Logic & Formulas Reference

This document provides a clean, comprehensive reference of all mathematical formulas, decision trees, statistical models, and allocation rules implemented in `replen_v2.py`.

---

## 1. Statistical & Demand Modeling Formulas

### 1.1 Mean Weekly Demand ($\mu_d$)
$$\mu_d = \frac{1}{N} \sum_{i=1}^N d_i$$
- $d_i$: Sales quantity in week $i$.
- $N$: Total number of historical weeks.

### 1.2 Demand Standard Deviation (`std_qty` or $\sigma_d$)
Calculated as population standard deviation ($\text{ddof} = 0$):
$$\sigma_d = \sqrt{\frac{1}{N} \sum_{i=1}^N (d_i - \mu_d)^2}$$

### 1.3 Coefficient of Variation (`cv` or $CV$)
$$\text{CV} = \frac{\sigma_d}{\mu_d}$$

Used to classify product demand volatility into **XYZ Classes**:
- **X Class (Steady)**: $\text{CV} < 0.5$
- **Y Class (Moderate)**: $0.5 \le \text{CV} < 1.0$
- **Z Class (Volatile / Intermittent)**: $\text{CV} \ge 1.0$ or undefined ($\mu_d = 0$)

---

## 2. Demand Forecasting (`forecast_qty`)

The forecasting algorithm automatically selects the method based on the XYZ volatility class:

### 2.1 Non-Z Classes (X & Y) — Simple Exponential Smoothing (SES)
$$\hat{d}_t = \alpha \cdot d_t + (1 - \alpha) \cdot \hat{d}_{t-1} \quad (\text{where } \alpha = 0.5)$$

### 2.2 Z Class — Croston Method (Intermittent Demand)
$$\text{Forecast Qty} = \frac{\bar{d}_{\text{non-zero}}}{\bar{p}_{\text{interval}}} = \frac{\left( \frac{\sum d_{\text{non-zero}}}{\text{Count of non-zero weeks}} \right)}{\left( \frac{\text{Total weeks}}{\text{Count of non-zero weeks}} \right)}$$

---

## 3. Service Level Z-Score Mapping (`z_value`)

Each ABC-XYZ matrix combination maps to a specific statistical $Z$-score:

$$\begin{array}{|c|c|c|}
\hline
\text{Classification} & \text{Service Level Target} & Z\text{-Score } (z\_value) \\
\hline
\text{AX} & 97.0\% & 1.88 \\
\text{AY} & 95.0\% & 1.65 \\
\text{AZ} & 93.0\% & 1.48 \\
\hline
\text{BX} & 91.0\% & 1.34 \\
\text{BY} & 90.0\% & 1.28 \\
\text{BZ} & 89.0\% & 1.23 \\
\hline
\text{CX} & 85.0\% & 1.04 \\
\text{CY} & 80.0\% & 0.84 \\
\text{CZ} & 75.0\% & 0.67 \\
\hline
\text{DX / DY / DZ} & 72.0\% & 0.58 \\
\text{EX / EY / EZ} & 70.0\% & 0.52 \\
\hline
\end{array}$$

---

## 4. Safety Stock & Inventory Targets

### 4.1 Safety / Buffer Stock (`buffer_stock`)
$$\text{Buffer Stock} = \left\lceil Z \times \sigma_d \times \sqrt{L} \right\rceil$$

- $Z$: Service level factor (`z_value`).
- $\sigma_d$: Weekly demand standard deviation (`std_qty`).
- $L$: Replenishment Lead Time in weeks (`lead_time`, default = $1.0$, configurable in Special Config).

### 4.2 Minimum Stock Level (`minimum_stock`)
$$\text{Minimum Stock} = \left\lceil \text{Forecast Qty} + \text{Buffer Stock} \right\rceil$$

### 4.3 Maximum Stock Level (`maximum_stock`)
$$\text{Maximum Stock} = \left\lceil \text{Minimum Stock} + \text{Forecast Qty} \right\rceil = \left\lceil 2 \cdot \text{Forecast Qty} + \text{Buffer Stock} \right\rceil$$

---

## 5. Effective Inventory Calculation

$$\text{Effective Qty} = \text{Total Qty} + \text{In-Transit Qty}$$

- **Total Qty**: Physical stock on hand at pharmacy (sales units / strips).
- **In-Transit Qty**: Shipped stock not yet checked in (sales units / strips).

---

## 6. Target Stock Selection (`replenish_mode`)

$$\text{Target Stock} = \begin{cases} \text{Maximum Stock}, & \text{if } \text{Mode} = \text{"max"} \\ \text{Minimum Stock}, & \text{if } \text{Mode} = \text{"min"} \end{cases}$$

---

## 7. Initial Net Replenishment Need (`replenish_value`)

### Decision Rules

1. **Overstock**: $\text{Total Qty} > \text{Target Stock} \implies \text{Status} = \text{"Overstock"}$
2. **Sufficient Stock**: $\text{Total Qty} \ge \text{Minimum Stock} \implies \text{Status} = \text{"No need to replenish"}$
3. **Not in Planogram / Master**: Item not present in POG master $\implies \text{Status} = \text{"Manually Review"}$
4. **Planogram No Follow**: `plano_follow == "no"` $\implies \text{Status} = \text{"No Follow"}$
5. **Gross Shortage Calculation**:

$$\text{Normal Qty} = \max(\text{Target Stock} - \text{Effective Qty}, \, 0)$$

$$\text{Replenish Value} = \begin{cases} \text{"No need to replenish"}, & \text{if } \text{Normal Qty} \le 0 \\ \text{Normal Qty}, & \text{if } \text{Normal Qty} > 0 \end{cases}$$

---

## 8. Unit Conversion (Sales Units → Ordering Units)

Converts sales units (e.g., strips, pieces) into supplier ordering units (e.g., boxes).

$$\text{Converted Qty} = \max\left( \left\lceil \frac{\text{Replenish Value}}{\text{Conversion Factor}} \right\rceil, \, 0 \right)$$

- **Conversion Factor**: Number of sales units per 1 box/ordering unit.
- **$\lceil \cdot \rceil$ (Ceiling Function)**: Rounds up to ensure only full box quantities are ordered.

---

## 9. Planogram Target Gap (`target_gap`)

Planogram unit quantity ($\text{Plano Unit Qty}$) is defined in **ordering units (boxes)**, while effective stock ($\text{Effective Qty}$) is in **sales units (strips)**. 

$$\text{Effective Qty (Boxes)} = \frac{\text{Effective Qty}}{\text{Conversion Factor}}$$

$$\text{Target Gap} = \max\left( \left\lceil \text{Plano Unit Qty} - \text{Effective Qty (Boxes)} \right\rceil, \, 0 \right)$$

---

## 10. Planogram Gap Override

$$\text{Final Status} = \begin{cases} \text{Target Gap}, & \text{if } \text{Target Gap} > \text{Converted Qty} \\ \text{Converted Qty}, & \text{otherwise} \end{cases}$$

> **Special Rule**: If $\text{Plano Unit Qty} = 0$, the order status is forced to `"No need to replenish"` and $\text{Final Order Qty} = 0$.

---

## 11. Hold Rule Overrides

1. **Hold**: $\text{Final Status} = \text{"Hold"}, \, \text{Ordered Qty Final} = 0$
2. **Hold to Stock-out (Hold to Zero)**: If $\text{Effective Qty} > 0 \implies \text{Final Status} = \text{"Hold to Zero"}, \, \text{Ordered Qty Final} = 0$
3. **Hold to Planogram**: Sets $\text{Ordered Qty Final} = \text{Target Gap}$.

---

## 12. Warehouse (TMP) Stock Pro-rating & Allocation

When central warehouse stock ($\text{TMP Qty}$) is insufficient across requesting pharmacies:

1. **Stockout TMP**: $\text{TMP Qty} = 0 \implies \text{Final Status} = \text{"Stockout TMP"}, \, \text{Ordered Qty Final} = 0$
2. **Proportional Share**:
   $$\text{Share}_p = \text{Requested Qty}_p \times \left( \frac{\text{TMP Qty}}{\sum_i \text{Requested Qty}_i} \right)$$
3. **Base Integer Allocation**: $\text{Base Allocation}_p = \lfloor \text{Share}_p \rfloor$
4. **Remainder Distribution**:
   $$\text{Remaining Boxes} = \text{TMP Qty} - \sum_p \lfloor \text{Share}_p \rfloor$$
   *Distributed 1 box at a time sorted by highest remainder $(\text{Share}_p - \lfloor \text{Share}_p \rfloor)$ with pharmacy ID tie-breaker.*

---

## 13. Flagged SKU Threshold (Dashboard KPI)

$$\text{Flag Ratio} = \frac{\text{Replenishment Value (IDR)}}{\text{L7D Sales Value (IDR)}} = \frac{\text{Ordered Qty Final} \times \text{Unit Price}}{\text{L7D Sales Qty} \times \text{Unit Price}}$$

$$\text{Flagged} = \begin{cases} \text{True}, & \text{if } \text{Flag Ratio} > 0.30 \text{ and Status} = \text{"Ordered"} \\ \text{False}, & \text{otherwise} \end{cases}$$

---

## Summary Matrix of Status Outputs

| Condition | `final_status` Output | `ordered_qty_final` |
| :--- | :--- | :--- |
| $\text{Total Qty} > \text{Target Stock}$ | `"Overstock"` | `0` |
| $\text{Total Qty} \ge \text{Minimum Stock}$ | `"No need to replenish"` | `0` |
| $\text{Normal Qty} \le 0$ | `"No need to replenish"` | `0` |
| `plano_follow == "no"` | `"No Follow"` | `0` |
| Item not in POG Master | `"Manually Review"` | `0` |
| Hold Rule Active | `"Hold"` | `0` |
| Hold to Stock-out & $\text{Effective Qty} > 0$ | `"Hold to Zero"` | `0` |
| Warehouse Stockout ($\text{TMP Qty} = 0$) | `"Stockout TMP"` | `0` |
| Valid Order | Numeric String (e.g. `"5"`) | `5` (or allocated integer) |
