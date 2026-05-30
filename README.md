# necis-downloader

Automated download of seismic waveforms from the
[KMA NECIS portal](https://necis.kma.go.kr) (National Earthquake Comprehensive
Information System) using Python and Playwright.

> **Status**
> Continuous waveforms ✅ working &nbsp;·&nbsp;
> Event waveforms ✅ working

---

## Overview

NECIS does not provide a direct download API. Instead, it serves a JavaScript
SPA where users submit a download job, wait for the server to package the data,
and then retrieve it from a history page. This tool automates that workflow:

1. **Submit** a download request via Playwright browser automation
2. **Poll** the history JSON API until the zip is ready (~10–30 min for full day)
3. **Download** the zip via HTTP (session cookies, with resume on connection drops)
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

### All stations, one day (default — same as daily cron)

```bash
python download_continuous.py \
    --date 2026-01-15 \
    --channels HHZ,HHN,HHE \
    --continuous-dir /data/necis/continuous
```

Submits a single request for all 404 KS stations. The server packages one large
zip (~10 GB); the download resumes automatically if the connection drops.

### Specific stations

```bash
python download_continuous.py \
    --date 2026-01-15 \
    --stations ADOA,AGSA,ANDB \
    --channels HHZ,HHN,HHE \
    --continuous-dir /data/necis/continuous
```

### Date range (historical backfill)

```bash
nohup ./archive_necis.sh 2026-01-01 2026-01-31 \
    >> logs/archive_necis.log 2>&1 &
tail -f logs/archive_necis.log
```

`archive_necis.sh` splits the request into batches of 2 stations to keep each
zip small and reliable. Use this for bulk backfill; use `download_continuous.py`
directly for day-by-day downloads.

### Resume a failed download (without re-requesting)

If the download failed mid-transfer and the job is still shown as complete in
the NECIS history, use `--fetch-only` to skip the request step and resume:

```bash
python download_continuous.py \
    --fetch-only \
    --poll-interval 60 \
    --max-wait 7200 \
    --organize \
    --continuous-dir /data/necis/continuous
```

### Daily cron job

```bash
crontab -e
# Add (runs at 01:00 KST every day):
0 1 * * * /path/to/necis-downloader/run_daily.sh >> /var/log/necis.log 2>&1
```

`run_daily.sh` downloads yesterday's data for all available NECIS stations
using the single-request approach above.

---

## Output Layout

```
data/necis/continuous/
  2026/
    ADOA/
      KS.ADOA.HHZ.2026.015.00.00.00   ← miniSEED, no file extension
      KS.ADOA.HHN.2026.015.00.00.00
      KS.ADOA.HHE.2026.015.00.00.00
    AGSA/
      KS.AGSA.HHZ.2026.015.00.00.00
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

Shell environment variables for `run_daily.sh` and `archive_necis.sh`:

| Variable | Default | Description |
|---|---|---|
| `CONTINUOUS_DIR` | `data/necis/continuous` | Organized output root |

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
       └─ For each day:
            ├─ continuous.py: fill form, click "다운로드 요청" → job queued
            ├─ fetch_downloads.py: poll requestFilesHisAjax.do until status="C"
            │    └─ requests.Session (with Playwright cookies):
            │         stream-GET zip, resume via HTTP Range on connection drops
            └─ utils.py: extract zip → organize into YYYY/STA/
```

The `submitted_after` timestamp filters the history API to the newly submitted
job, avoiding accidental re-downloads of old queued jobs.

---

## `download_continuous.py` Options

```
--date YYYY-MM-DD       Single day (overrides --start/--end)
--start / --end         Date range (default: yesterday)
--stations STA1,STA2    Station codes; omit to request all NECIS stations
--station-csv PATH      Load station list from CSV (must be set explicitly)
--network KS            Filter station CSV to network (default: KS)
--channels HHZ,HHN,HHE Channel codes (default: HHZ,HHN,HHE)
--batch-size N          Max stations per request (default: all at once)
--continuous-dir PATH   Organized output root
--poll-interval SEC     Seconds between polls (default: 30)
--max-wait SEC          Max wait for server (default: 600)
--fetch-only            Skip request; download already-queued files
--no-fetch              Submit only; skip polling/download
--no-organize           Download only; skip zip extraction
```

---

## Event Waveforms

`download_events.py` downloads event waveforms from the NECIS earthquake catalog
page and organizes them to match the `kma_waveforms` directory layout used in
KMA seismological workflows.

### Catalog formats

**Jangsung / split-column format (KST times):**
```
Year,Month,Day,Hour,Minute,Second,Latitude,Longitude,Depth,Magnitude
2023,5,26,20,39,54,35.46,126.81,8,1.1
```
Times are interpreted as KST and converted to UTC automatically.

**KMA catalog format (UTC datetime):**
```
datetime,event_id,...
2016-01-01 17:27:10,201601_0001,...
```

### Usage

```bash
# Jangsung-style catalog, all events
python download_events.py \
    --catalog event_catalog.csv

# KMA catalog, date/magnitude filter, acceleration only
python download_events.py \
    --catalog meta/catalog_KMA_20160101-20260203.csv \
    --start 2024-01-01 --end 2024-12-31 \
    --min-mag 2.0 \
    --data-type a

# Velocity only, skip SAC conversion
python download_events.py \
    --catalog event_catalog.csv \
    --data-type v --no-convert-sac
```

`--stations` and `--pre`/`--post` are accepted for API compatibility but have
no effect — NECIS event ZIPs include all available KS stations with the server's
default time window.

### Output layout

```
data/necis/events/
  20230526113954/            ← UTC origin time (YYYYMMDDHHmmss)
    2023002939.a/            ← NECIS ID + .a (acceleration)
      MSEED/
        KS.ADOA.HGE.2023.146.11.39.54
        KS.ADOA.HGZ.2023.146.11.39.54
        ...
      SAC/
        BG/  KS.ADOA..BGE.D.2023.146....SAC
        HG/  KS.ADOA..HGE.D.2023.146....SAC
    2023002939.v/            ← same NECIS ID + .v (velocity)
      MSEED/  ...
      SAC/  BH/  HH/  ...
```

The outer directory name is the UTC origin time; the inner name is the NECIS
internal event ID (e.g. `2023002939`) with `.a` or `.v` suffix. SAC files are
sorted into band sub-directories (`BG/`, `HG/`, `HH/`, …) matching the standard
`kma_waveforms` layout.

### `download_events.py` options

```
--catalog PATH          Path to event catalog CSV (required)
--start YYYY-MM-DD      Filter by UTC origin time (≥)
--end   YYYY-MM-DD      Filter by UTC origin time (≤)
--min-mag FLOAT         Skip events below this magnitude (default: 0)
--data-type {a,v,both}  Acceleration, velocity, or both (default: both)
--convert-sac           Convert miniSEED → SAC via mseed2sac (default: on)
--no-convert-sac        Skip SAC conversion
--output-dir PATH       Organized output root (default: data/necis/events)
--poll-interval SEC     Seconds between download queue polls (default: 30)
--max-wait SEC          Max wait per download job (default: 600)
```

---

## Project Structure

```
necis-downloader/
├── necis/                    Python package
│   ├── __init__.py           Public API surface
│   ├── config.py             NECISConfig — credentials and paths
│   ├── browser.py            Playwright browser client (login, navigation)
│   ├── continuous.py         Continuous waveform request submission
│   ├── fetch_downloads.py    History API polling + HTTP download (with resume)
│   ├── utils.py              Zip extraction, miniSEED organization
│   └── events.py             Event waveform pipeline
├── download_continuous.py    CLI: continuous waveforms
├── download_events.py        CLI: event waveforms
├── discover_necis.py         Browser inspection / API capture tool
├── archive_necis.sh          Batch archive script (date range, 2-station batches)
├── run_daily.sh              Cron wrapper (all stations, yesterday's data, 01:00 KST)
├── 01.Test_pipeline.ipynb    Interactive test notebook
├── requirements_necis.txt    Python dependencies
├── pyproject.toml            Package metadata
├── .env.example              Credential template
└── docs/
    └── guide.md              Detailed usage guide and troubleshooting
```

---

## Limitations

- **Daily quota:** NECIS enforces ~10 GB/day (and ~30 GB over any 3-day window).
  One full day of all 404 KS stations ≈ 9.5–10.7 GB, consuming the entire daily
  allowance. Plan archive work accordingly (~1 calendar day per real day).
- **Server throughput:** The NECIS FTP server delivers at ~0.5 MB/s and
  frequently drops connections mid-transfer. The downloader resumes via HTTP
  Range requests; expect 6–12 hours to download a full day of all-station data.
- Requires an active NECIS account (KMA employee or registered researcher).

---

## License

[MIT](LICENSE)
