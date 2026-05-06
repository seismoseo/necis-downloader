# NECIS Waveform Downloader — Detailed Guide

## Overview

Automated download of continuous waveforms from <https://necis.kma.go.kr> using
a Playwright-based browser automation pipeline.

**Three-step workflow:**

1. Submit a download request via the NECIS web interface
2. Poll the server until the zip is ready (~2–10 min), then download
3. Extract zip and organize miniSEED files into `YYYY/STA/` structure

Files land at:
```
<continuous-dir>/YYYY/STA/NET.STA.CHA.YYYY.DDD.HH.MM.SS
```

---

## Setup (first time only)

```bash
pip install -r requirements_necis.txt
playwright install chromium

cp .env.example .env
# Edit .env and set:
#   NECIS_USER=your_email@example.com
#   NECIS_PASS=your_password
```

---

## Daily Download (single day)

Download yesterday's data for specific stations:

```bash
python download_continuous.py \
    --stations ADOA,AGSA \
    --channels HHZ,HHN,HHE \
    --continuous-dir /path/to/data/necis/continuous
```

| Option | Description |
|--------|-------------|
| `--date YYYY-MM-DD` | Specific date (default: yesterday) |
| `--stations STA1,STA2` | Station codes, no network prefix |
| `--channels HHZ,HHN,HHE` | Channel codes (last letter → component Z/N/E) |
| `--continuous-dir PATH` | Output root directory |
| `--batch-size N` | Stations per request (default: all; use 2 for 3-component 100 Hz) |
| `--network KS` | Filter station CSV to network (default: KS) |

The script automatically submits the request, polls every 30 s until the server
finishes preparing the zip (up to 10 min), downloads it, and organizes files
into `YYYY/STA/` after each batch.

---

## Archive Download (date range)

Download all days from 2026-01-01 to today, all KS stations:

```bash
mkdir -p logs
nohup ./archive_necis.sh 2026-01-01 >> logs/archive_necis.log 2>&1 &
tail -f logs/archive_necis.log
```

The script loops day by day, batch by batch: submit → poll/download → organize → next batch.

**Estimated time:** ~203 batches/day × ~4 min/batch → ~13 h per calendar day of data.
Use `nohup` or `screen`/`tmux` so it keeps running after logout.

---

## Daily Automated Cron Job

Add to crontab (runs at 04:30 every morning):

```bash
crontab -e

# Download previous day's data at 04:30
30 4 * * * /path/to/necis-downloader/run_daily.sh >> /var/log/necis.log 2>&1
```

`run_daily.sh` uses yesterday's date automatically and delegates to `archive_necis.sh`.

---

## Notes and Limits

- NECIS enforces a **~40 MB per-download cap**. For all 404 KS stations, one day
  of 3-component 100 Hz data is ~5 GB. `archive_necis.sh` uses `--batch-size 2`
  (default) to stay safely under the cap.

- **Output format:** miniSEED (STEIM2). NECIS filenames have no file extension.
  Pattern: `NET.STA.CHA.YYYY.DDD.HH.MM.SS`
  Example: `KS.ADOA.HGZ.2026.125.00.00.00`

- **Channel selection:** The NECIS form has only E/N/Z component checkboxes —
  it delivers whatever channel type (HH\*, HG\*, EL\*) the station has recorded.
  Requesting `HHZ,HHN,HHE` via `--channels` maps to components Z, N, E and
  automatically captures all 100 Hz channel types.

- **Debug screenshots** are saved to `data/necis/debug_*.png` on failure.
  Set `NECIS_HEADLESS=0` to watch the browser in real time.

---

## Troubleshooting

**"Request submitted: False"**
The modal confirm button was not found. Check the screenshot in `data/necis/`.
Run with `NECIS_HEADLESS=0` to see what the browser is doing.

**"Downloaded 0 file(s)"**
The server may still be processing. The poller waits at least 2 polls (minimum
30 s) before giving up. Try increasing `--max-wait` (default 600 s) or check
the NECIS history page manually.

**"PermissionError: /data"**
The output directory does not exist or is not writable. Use `--continuous-dir`
to specify a path you own.

**"Cannot parse year/station from filename"**
Unexpected filename format from NECIS. Check `data/necis/zips/_staging/` to
see the actual filenames and update `_SEED_PAT` in `necis/utils.py` if needed.

---

## File Reference

| File | Description |
|------|-------------|
| `download_continuous.py` | Main CLI for continuous waveforms |
| `download_events.py` | CLI for event waveforms |
| `archive_necis.sh` | Shell wrapper for long-running archive downloads |
| `run_daily.sh` | Cron wrapper for daily automation |
| `discover_necis.py` | Browser inspection tool — captures API calls |
| `necis/continuous.py` | Browser automation: submit request |
| `necis/fetch_downloads.py` | Poll history API and download via requests |
| `necis/utils.py` | Extract zips and organize into `YYYY/STA/` |
| `necis/browser.py` | Playwright browser client (login, navigation) |
| `necis/config.py` | `NECISConfig`: reads credentials from `.env` |
| `necis/events.py` | Event waveform pipeline (selectors need ADAPT pass) |
| `01.Test_pipeline.ipynb` | Interactive notebook for testing |
