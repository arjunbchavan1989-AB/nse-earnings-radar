# NSE Earnings Radar

A self-updating dashboard for NSE (Indian market) quarterly earnings:
**sales YoY, EPS YoY, beat/miss vs consensus, day-1 market reaction,
post-earnings drift (PEAD), and delayed positive reactions.**

Live site: `https://<account>.github.io/nse-earnings-radar/`

## What it shows

For every earnings announcement in the tracked universe (183 liquid NSE
large/mid caps; ~178 have earnings data on Yahoo Finance) over the last ~2
years:

- **Sales YoY / EPS YoY** — reported quarter vs the same fiscal quarter a year
  earlier, consolidated, in Rs crore (screener.in). Sign-flipping EPS
  (loss-to-profit etc.) is labelled explicitly instead of showing a %.
- **Surprise (beat/miss)** — actual EPS vs analyst consensus estimate
  (Yahoo Finance). "no est." means no verified consensus exists for that
  quarter — nothing is imputed.
- **Day-1 reaction** — close of the first session that fully follows the
  announcement vs the prior close. After-close and weekend releases roll to
  the next session.
- **Drift +5/+10/+20** — post-earnings announcement drift (PEAD): close 5/10/20
  trading sessions after the reaction session vs the reaction close. Windows
  that haven't fully elapsed show as pending.
- **Delayed+** — screening rule (not a predictive signal): day-1 reaction
  was flat or negative, then the stock closed above the reaction session's
  high within the next 5 sessions — a sign the market digested the print
  positively after initial hesitation. The trigger day is in the detail view.

The **Upcoming tab** lists scheduled announcements for the next 90 days with
consensus EPS estimates where available.

## Files

- `index.html` — the dashboard (static, no build step)
- `data/earnings.json` — generated dataset (committed by the workflow)
- `pipeline.py` — data pipeline (Python)
- `tickers.txt` — tracked universe, one NSE symbol per line
- `.github/workflows/update.yml` — daily refresh + GitHub Pages deploy

## Auto-update schedule

GitHub Actions refreshes the data **daily at 19:30 IST (14:00 UTC)**,
commits the new `data/earnings.json`, and redeploys GitHub Pages. The
workflow can also be triggered manually (Actions tab -> Update earnings
data -> Run workflow).

## Running locally

```bash
pip install -r requirements.txt
python3 pipeline.py            # full universe, writes data/earnings.json
python3 pipeline.py --workers 2
```

Useful options:

- `--tickers my_list.txt` — custom universe
- `--out data/earnings.json` — output path
- `--workers N` — parallel fetchers (keep low to avoid rate limits)

Then open `index.html` directly, or serve the folder
(`python3 -m http.server`) and browse to it.

## Data sources & caveats

- Announcement dates, EPS estimates, surprise %, adjusted prices, sector:
  Yahoo Finance (via yfinance)
- Quarterly sales/EPS history (Rs crore, consolidated): screener.in
- Consensus coverage is best for large caps; mid caps often show "no est."
- Yahoo's announcement timestamps are approximate; the reaction session is
  derived as the first session whose close follows the announcement.
- Prices are dividend/split-adjusted daily closes.
- screener.in occasionally rate-limits automated fetches; if that happens the
  pipeline logs a warning and the affected stocks show blank sales/EPS YoY
  until the next successful run.

This is a research/educational tool, not investment advice.
