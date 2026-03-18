# Minimal Model and Data Collection Plan

## Goal

Build a small, robust, and low-maintenance model loop:

1. Collect per-run feature samples from live analysis.
2. Auto-label closed-month samples using Binance monthly high/low.
3. Train a minimal model:
   - Probability calibration (binning + Laplace smoothing)
   - Optional cost model (linear sigma) when observed cost labels exist

## Data Flow

1. `position_analyze.py` runs and builds `profit_optimization_context`.
2. `services/model_data.append_model_samples(...)` appends rows to:
   - `logs/model_samples.jsonl`
3. Train model with:
   - `python scripts/train_minimal_market_model.py`
4. Model artifact written to:
   - `models/minimal_market_model.json`

## Sample Schema (JSONL row)

Core fields collected now:

- `run_ts_utc`
- `event_name`
- `contract_month` (`YYYY-MM`)
- `question`
- `direction_in_question` (`above` / `below`)
- `strike`
- `model_prob_yes`
- `implied_prob_yes`
- `best_side`, `best_side_price`, `best_side_edge`
- `current_btc_price`
- `days_left_in_month`
- `drawdown_from_month_high_pct`
- `space_to_reclaim_target_pct`
- `market_regime`
- `atr_pct`
- `realized_vol_daily_pct`
- `mu_return`, `sigma_return`
- `total_cost_prob`
- `fractional_kelly`
- `suggested_max_alloc_usdc`
- `label_yes` (optional, default null)

Optional future labels:

- `observed_cost_prob` (for cost model)
- `realized_edge` (for model evaluation)

## Labeling Rule (auto in trainer)

For closed months only:

- If `direction_in_question == "above"`:
  - `label_yes = 1` if monthly high >= strike else `0`
- If `direction_in_question == "below"`:
  - `label_yes = 1` if monthly low <= strike else `0`

Monthly high/low is pulled from Binance daily klines for that month.

## Model Definition

### Probability Calibration

- Method: fixed bins on raw probability (`model_prob_yes`)
- Smoothing: `(pos + 1) / (n + 2)`
- Output: per-bin calibrated probability table

### Cost Model (optional)

- Feature: sigma proxy (`realized_vol_daily_pct` fallback `atr_pct`)
- Target: `observed_cost_prob`
- Method: simple linear regression
- Fallback defaults used when label rows are insufficient

## Operational Notes

- Keep collection append-only (`logs/model_samples.jsonl`).
- Retrain daily or every 4h after analysis batch.
- Use minimum quality gate before production usage:
  - `prob_label_rows >= 100`
  - calibration curve monotonic check (manual review first)
- Keep model versioned by filename if needed.

## Commands

Collecting is automatic during `position_analyze.py` run.

Train model:

```bash
python scripts/train_minimal_market_model.py
```

Custom paths:

```bash
python scripts/train_minimal_market_model.py --samples-path logs/model_samples.jsonl --output-path models/minimal_market_model.json --bins 10
```
