# Oil OB

Standalone real-time web dashboard for tracking CME crude oil futures.

## What it tracks

- WTI Crude Oil Futures (`CL=F`)
- Brent Crude Oil Futures (`BZ=F`)
- WTI-Brent spread
- Intraday 1-minute WTI line chart
- Auto alert thresholds (WTI/Spread up/down levels, browser-side)

## Run

From the project root:

```bash
python oil_ob/app.py
```

Open in browser:

`http://127.0.0.1:5050`

## Optional env vars

- `OIL_OB_HOST` (default: `0.0.0.0`)
- `OIL_OB_PORT` (default: `5050`)

## Notes

- Data source is Yahoo chart API for futures symbols, refreshed every ~1 second.
- Quotes are delayed by the upstream data provider.
