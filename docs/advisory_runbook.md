# Advisory (Fair-Value Rebalancer) Runbook

> Operational guide for the **advisory-only** branch (`fair-value-advisory`).
> Reference design: session plan-advisory.md v1.3 FROZEN.

This runbook covers day-to-day operations of the advisory pipeline:
periodic batch generation, settlement refresh, alert email, dashboard,
and the "what to do when things break" flows. **Advisory mode does not
place any orders** — every operation is read-only against Polymarket /
Binance and only writes to PG.

---

## 1. Components & systemd units

| Component | systemd unit | Cadence | Source script |
|---|---|---|---|
| Batch runner (R1) | `auto-poly-advisory-batch.service` | every 300s | `scripts/advisory_batch_runner.py` |
| Settlement refresher (R2) | `auto-poly-advisory-settlement.service` | every 600s | `scripts/advisory_settlement_refresher.py` |
| Metrics + alert email (R4) | `auto-poly-advisory-metrics.timer` → `.service` | every 300s (oneshot) | `scripts/advisory_metrics.py --check --alert-email` |
| Dashboard page | (shares `auto-poly-app.service`) | on demand | route `/recommendations` (services/advisory/dashboard.py) |

All advisory units set `Environment=LD_PRELOAD=` to bypass the
system-wide `proxychains` preload — without this the PG connection over
the LAN times out (see repo memory).

---

## 2. Install / first-time setup

```bash
# 1. Verify schema is in shape (idempotent).
LD_PRELOAD="" uv run scripts/advisory_verify_schema.py --apply

# 2. Install / refresh systemd units.
sudo bash scripts/install_systemd.sh

# 3. Confirm everything is running.
systemctl status auto-poly-advisory-batch auto-poly-advisory-settlement \
                 auto-poly-advisory-metrics.timer

# 4. Smoke the batch path.
LD_PRELOAD="" uv run scripts/advisory_batch_runner.py --once --max-strikes 4
```

---

## 3. Day-to-day operations

### 3.1 Status / logs

```bash
# Live tail
journalctl -u auto-poly-advisory-batch -f
journalctl -u auto-poly-advisory-settlement -f

# Rotated file logs (20MB x5 backups)
tail -f logs/advisory_batch_runner.log
tail -f logs/advisory_settlement_refresher.log

# Most recent metrics + alert decision
journalctl -u auto-poly-advisory-metrics --since "1 hour ago"
LD_PRELOAD="" uv run scripts/advisory_metrics.py        # snapshot, no email
LD_PRELOAD="" uv run scripts/advisory_metrics.py --json # machine-readable
```

### 3.2 Restart / stop

```bash
systemctl restart auto-poly-advisory-batch
systemctl restart auto-poly-advisory-settlement
systemctl restart auto-poly-advisory-metrics.timer

systemctl stop auto-poly-advisory-batch     # halts batch generation
systemctl start auto-poly-advisory-batch
```

> **Always use systemctl.** Do not start the runners with `nohup`/manually
> while the systemd unit is enabled — you'll get duplicate processes
> writing duplicate batches.

### 3.3 Tuning runtime params

Override interval / max-strikes / slug via environment in `.env` (read by
`EnvironmentFile=` in the unit):

```dotenv
# .env
ADVISORY_BATCH_INTERVAL=180          # tighter cadence (default 300)
ADVISORY_BATCH_MAX_STRIKES=8         # more strikes per batch (default 6)
ADVISORY_SETTLEMENT_INTERVAL=120     # near month-end, faster refresh
ADVISORY_SETTLEMENT_MAX_STRIKES=10   # superset coverage
# ADVISORY_BATCH_SLUG=what-price-will-bitcoin-hit-in-june-2026   # pin slug
```

Then `systemctl restart auto-poly-advisory-batch`.

---

## 4. Metrics + alerting (R4)

The metrics oneshot runs every 5 min via the timer. It evaluates 5
thresholds and, when any fires, sends ONE alert email to `TO_EMAIL`
(SMTP via `notifications/email.py`).

| Metric | Default threshold | What it means when it fires |
|---|---|---|
| `batch_freshness_seconds` | > 600s | Batch runner is dead or wedged; check `journalctl -u auto-poly-advisory-batch` |
| `batch_failure_rate` (6h) | > 0.10 | Pipeline failing too often; inspect `failure_step` / `failure_error` in `market_view_batches` |
| `missing_condition_count` | > 0 | Settlement adapter could not resolve some `condition_id`s in gamma; usually transient |
| `disputed_count` | > 0 | Polymarket flagged a market as disputed → manual review required |
| `state_flips_24h` | > 0 | A `condition_id` changed `settlement_state` between versions (e.g. settled → disputed) |

Anti-spam: alert fingerprint = sorted `(metric, severity)`; same
fingerprint within 1h cooldown is suppressed. State persists in
`logs/.advisory_alert_state.json` (atomic replace).

### 4.1 Exit codes

* `0` — no alerts.
* `2` — alerts present (when invoked with `--check`). NOT a systemd
  failure: the unit declares `SuccessExitStatus=0 2`.

### 4.2 Manual mute / un-mute

To suppress alerts during a planned maintenance window:

```bash
# Disable the timer (alerts paused)
systemctl stop auto-poly-advisory-metrics.timer

# When done
systemctl start auto-poly-advisory-metrics.timer
```

To force the next tick to send (e.g. after fixing the underlying issue
but before the cooldown expires):

```bash
rm -f logs/.advisory_alert_state.json
```

---

## 5. Schema drift / verification

```bash
# Read-only verify (default).
LD_PRELOAD="" uv run scripts/advisory_verify_schema.py
# JSON for CI / paste into incident ticket
LD_PRELOAD="" uv run scripts/advisory_verify_schema.py --json

# Apply (idempotent CREATE TABLE IF NOT EXISTS).
LD_PRELOAD="" uv run scripts/advisory_verify_schema.py --apply
```

Exit `1` indicates a drift report (missing table or enum CHECK
mismatch). The script does NOT auto-fix CHECK drift — repair flow:

```bash
# 1. Backup the affected table.
pg_dump -h <host> -U <user> -t <table> <db> > backup_<table>_$(date +%s).sql

# 2. In a transaction, replace the CHECK.
psql <conn> <<'SQL'
BEGIN;
ALTER TABLE <table> DROP CONSTRAINT <chk_name>;
ALTER TABLE <table> ADD CONSTRAINT <chk_name>
  CHECK (<col> IN ('val1','val2',...));
SQL

# 3. Re-verify.
LD_PRELOAD="" uv run scripts/advisory_verify_schema.py
```

---

## 6. Degraded paths (no panic situations)

| Symptom | Likely cause | Action |
|---|---|---|
| Dashboard shows "数据陈旧" banner | batch runner stalled | restart `auto-poly-advisory-batch`; tail logs |
| `refresh_status='partial'` for several iterations | one or more `condition_id`s dropped from gamma | usually self-heals on next refresh; investigate if persistent > 1h |
| `refresh_status='failed'` repeatedly | gamma network outage OR universe selector returned dead slug | check `select_active_month_slug` rollover; verify slug exists in browser |
| Batches succeed but `quoted` count is 0 | Polymarket CLOB outage | wait; advisory tolerates this (TokenQuote with None bid/ask) |
| `select_active_month_slug` falls back with warning | next month not yet listed in gamma | normal in early-month days; ignore unless current month also missing |
| Many "Could not create api key" warnings | analyze profile lacks write key | benign — only affects post-trade endpoints, prices still work |
| `batch_freshness` alert + batch_runner up | runner is alive but stuck on a single iteration | inspect last log line; if same descriptor for > 5min, restart |

---

## 7. Dashboard

* Main app: `https://<host>/` → top nav has **📊 Advisory ↗** which
  opens the standalone advisory page in a new tab.
* Direct URL: `/recommendations` (auth-gated, same session).
* API: `/api/advisory/recommendations` returns latest *complete* batch
  with staleness banner; `POST /api/advisory/manual_trades` records a
  user-side fill (no order placement, just bookkeeping).
* Staleness threshold: 5 min (hardcoded in
  `services/advisory/dashboard.py` `STALENESS_THRESHOLD`). When stale,
  the API returns `snapshots: []` to force the banner.

---

## 8. Stop everything (full halt)

```bash
systemctl stop auto-poly-advisory-batch \
               auto-poly-advisory-settlement \
               auto-poly-advisory-metrics.timer
systemctl disable auto-poly-advisory-batch \
                  auto-poly-advisory-settlement \
                  auto-poly-advisory-metrics.timer
```

Pipeline is now completely offline. Dashboard `/recommendations` will
serve the *last persisted* batch with a stale banner; nothing new will
be written.

---

## 9. References

* Plan: session plan-advisory.md v1.3 FROZEN (sections §4.1 schema, §5
  dashboard, §7 phases, §11 remaining work).
* Schema constants source-of-truth: `data/advisory_schema.py`
  (RESOLUTION_STATES, HALT_REASONS, FAIR_VALUE_STATUSES,
  SETTLEMENT_STATES, REFRESH_STATUSES, BATCH_STATUSES).
* Computer entry: `services/advisory/computer.py::run_advisory_batch`.
* Inputs assembly: `services/advisory/inputs.py`.
