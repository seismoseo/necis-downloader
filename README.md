# necis-downloader

Automated download of seismic waveforms from the
[KMA NECIS portal](https://necis.kma.go.kr) (National Earthquake Comprehensive
Information System) using Python and Playwright.

> **Status**
> Continuous waveforms ✅ working &nbsp;·&nbsp;
> Event waveforms ⚠️ selectors need one-time configuration
> (see [Event Waveforms](#event-waveforms))

---

## Overview

NECIS does not provide a direct download API. Instead, it serves a JavaScript
SPA where users submit a download job, wait for the server to package the data,
and then retrieve it from a history page. This tool automates that workflow:

1. **Submit** a download request via Playwright browser automation
2. **Poll** the history JSON API every 30 s until the zip is ready (~2–10 min)
3. **Download** the zip via HTTP session (with session cookies)
4. **Organize** miniSEED files into a `YYYY/STA/` directory tree

Supports both **continuous** (daily archive) and **event-based** waveform downloads
for the KMA KS-network (404 stations).

---

## Requirements

- Python ≥ 3.9
- Chromium — installed automatically via `playwright install chromium`
- Active [KMA NECIS](https://necis.kma.go.kr) account

---

## Installation

```bash
git clone <repository-url>
cd necis-downloader

pip install -r requirements_necis.txt
playwright install chromium

cp .env.example .env
# Edit .env — set NECIS_USER and NECIS_PASS
```

---

## Quick Start

### Single day, specific stations

```bash
python download_continuous.py \
    --date 2026-01-15 \
    --stations ADOA,AGSA,ANDB \
    --channels HHZ,HHN,HHE \
    --continuous-dir /data/necis/continuous
```

### All KS stations, date range (archive)

```bash
mkdir -p logs
nohup ./archive_necis.sh 2026-01-01 2026-12-31 >> logs/archive_necis.log 2>&1 &
tail -f logs/archive_necis.log
```

The script batches stations (2 per request) to stay under NECIS's 40 MB
per-request cap and prints an estimated completion time before starting.

### Daily cron job

```bash
crontab -e
# Add:
30 4 * * * /path/to/necis-downloader/run_daily.sh >> /var/log/necis.log 2>&1
```

---

## Output Layout

```
data/necis/continuous/
  2026/
    ADOA/
      KS.ADOA.HGZ.2026.001.00.00.00   ← miniSEED, no file extension
      KS.ADOA.HGN.2026.001.00.00.00
      KS.ADOA.HGE.2026.001.00.00.00
    AGSA/
      KS.AGSA.HGZ.2026.001.00.00.00
      ...
```

Filename pattern: `NET.STA.CHA.YYYY.DDD.HH.MM.SS`

---

## Configuration

Credentials are read from `.env` (copy `.env.example` → `.env`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `NECIS_USER` | ✅ | — | Login e-mail or username |
| `NECIS_PASS` | ✅ | — | Password |
| `NECIS_HEADLESS` | — | `1` | Set to `0` to watch the browser window |
| `NECIS_DOWNLOAD_DIR` | — | `data/necis` | Intermediate zip/staging root |

Shell environment variables for `archive_necis.sh`:

| Variable | Default | Description |
|---|---|---|
| `CONTINUOUS_DIR` | `data/necis/continuous` | Organized output root |
| `BATCH_SIZE` | `2` | Stations per NECIS request |
| `CHANNELS` | `HHZ,HHN,HHE` | Channel codes (→ E/N/Z components) |
| `STATION_CSV` | auto-detected | Path to `KP_station_list.csv` |

---

## Channel Selection

The NECIS form uses E/N/Z **component** checkboxes, not channel-prefix selectors.
Requesting `HHZ,HHN,HHE` (the default) maps to components Z, N, E and the server
automatically delivers whatever channel type each station has recorded —
`HH*` (broadband), `HG*` (strong-motion), or `EL*` (low-frequency). All 100 Hz
channels are retrieved in a single pass.

---

## How It Works

```
download_continuous.py
  └─ NECISBrowser (Playwright, headless Chromium)
       ├─ Login
       └─ For each station batch per day:
            ├─ continuous.py: fill form, click "다운로드 요청" → job queued
            ├─ fetch_downloads.py: poll requestFilesHisAjax.do (30 s interval)
            │    └─ requests.Session (with Playwright cookies): stream GET zip
            └─ utils.py: extract zip → organize into YYYY/STA/
```

The `submitted_after` timestamp filters the history API to the newly submitted
job, avoiding accidental re-downloads of old queued jobs.

---

## `download_continuous.py` Options

```
--date YYYY-MM-DD       Single day (overrides --start/--end)
--start / --end         Date range (default: yesterday)
--stations STA1,STA2    Station codes (no network prefix)
--station-csv PATH      KP_station_list.csv (auto-detected if omitted)
--network KS            Filter station CSV to network (default: KS)
--channels HHZ,HHN,HHE Channel codes (default: HHZ,HHN,HHE)
--batch-size N          Max stations per request (default: all at once)
--continuous-dir PATH   Organized output root
--poll-interval SEC     Seconds between polls (default: 30)
--max-wait SEC          Max wait for server (default: 600)
--no-fetch              Submit only; skip polling/download
--no-organize           Download only; skip zip extraction
```

---

## Event Waveforms

Event waveform download (`download_events.py` / `necis/events.py`) is
implemented but requires one-time selector configuration:

```bash
# 1. Run the discovery tool — browser opens so you can navigate to the event page
NECIS_HEADLESS=0 python discover_necis.py

# 2. Note the actual CSS selectors from the printed element list,
#    then update the ADAPT constants at the top of necis/events.py

# 3. Download events from a KMA catalog CSV
python download_events.py \
    --catalog meta/catalog_KMA_20160101-20260203.csv \
    --start 2026-01-01 --end 2026-03-31 \
    --min-mag 3.0 \
    --stations ADOA,AGSA \
    --pre 30 --post 120
```

Output: `data/necis/events/YYYY/<event_id>/NET.STA.CHA.*`

---

## Project Structure

```
necis-downloader/
├── necis/                    Python package
│   ├── __init__.py           Public API surface
│   ├── config.py             NECISConfig — credentials and paths
│   ├── browser.py            Playwright browser client (login, navigation)
│   ├── continuous.py         Continuous waveform request submission
│   ├── fetch_downloads.py    History API polling + HTTP download
│   ├── utils.py              Zip extraction, miniSEED organization
│   └── events.py             Event waveform pipeline
├── download_continuous.py    CLI: continuous waveforms
├── download_events.py        CLI: event waveforms
├── discover_necis.py         Browser inspection / API capture tool
├── archive_necis.sh          Archive shell script (date range, all stations)
├── run_daily.sh              Cron wrapper (downloads yesterday's data)
├── 01.Test_pipeline.ipynb    Interactive test notebook
├── requirements_necis.txt    Python dependencies
├── pyproject.toml            Package metadata
├── .env.example              Credential template
└── docs/
    └── guide.md              Detailed usage guide and troubleshooting
```

---

## Limitations

- **40 MB per-request cap** enforced by NECIS. At 100 Hz, 3 components ≈ 42 MB/station/day, so `--batch-size 2` is the practical maximum for full-component downloads.
- **Archive time:** 404 KS stations at batch-size 2 → ~203 requests/day × ~4 min = ~13 h per calendar day of data. A full year takes roughly 50 days of wall time.
- **Event selectors** in `necis/events.py` have `# ADAPT` placeholders that must be filled in after a live `discover_necis.py` run.
- Requires an active NECIS account (KMA employee or registered researcher).

---

## License

[MIT](LICENSE)
