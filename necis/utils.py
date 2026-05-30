"""
Utilities: unzip and organize NECIS miniSEED downloads.

After fetch_downloads saves ZIP files, this module:
  1. Extracts them (flat — strips any internal directory structure)
  2. Organizes miniSEED files into a date-based directory tree

Continuous layout:  out_root/YYYY/MM/DD/NET.STA.LOC.CHA.YYYY.DDD[.mseed]
Events layout:      out_root/<event_id>/NET.STA.LOC.CHA...

Date is parsed from SEED-style filenames (NET.STA.LOC.CHA.YYYY.DDD pattern).
If the filename doesn't match, file mtime is used as a fallback.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import zipfile

# bsdtar may only be in the base conda env, not the active pipeline env
_BSDTAR   = shutil.which("bsdtar")   or "/home/msseo/miniforge3/bin/bsdtar"
_MSEED2SAC = shutil.which("mseed2sac") or "/home/msseo/bin/mseed2sac"
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Matches NECIS filenames: NET.STA.CHA.YYYY.DDD[.HH.MM.SS]
# Location code is absent in NECIS output (e.g. KS.ADOA.HGZ.2026.125.00.00.00).
# Also handles the standard SEED variant with location: NET.STA.LOC.CHA.YYYY.DDD.
_SEED_PAT = re.compile(
    r"^[A-Z0-9]{1,2}\."          # network
    r"[A-Z0-9]{3,5}\."           # station
    r"(?:[A-Z0-9]{0,2}\.)?"      # location (optional — absent in NECIS files)
    r"[A-Z0-9]{2,3}\."           # channel (2- or 3-char)
    r"(?P<year>\d{4})\."         # year
    r"(?P<jday>\d{3})",          # Julian day
    re.IGNORECASE,
)


def _jday_to_date(year: int, jday: int) -> Optional[datetime]:
    try:
        return datetime(year, 1, 1) + timedelta(days=jday - 1)
    except (ValueError, OverflowError):
        return None


def extract_zips(
    zip_dir: Path,
    out_dir: Path,
    delete_zip: bool = False,
) -> list[Path]:
    """Extract all *.zip files from zip_dir into out_dir (flat layout).

    Parameters
    ----------
    zip_dir    : directory containing downloaded .zip files
    out_dir    : destination directory for extracted files
    delete_zip : remove the .zip after successful extraction

    Returns
    -------
    List of extracted file paths.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    zip_files = sorted(zip_dir.glob("*.zip"))

    if not zip_files:
        logger.info("No zip files found in %s", zip_dir)
        return []

    for zp in zip_files:
        z01 = zp.with_suffix(".z01")
        if z01.exists():
            # Split archive — unzip/zipfile don't support multi-disk ZIP; concatenate
            # the parts (.z01 then .zip) into a single file and extract with bsdtar.
            combined = zp.with_name(zp.stem + "_combined.zip")
            logger.info("Concatenating %s + %s → %s …", z01.name, zp.name, combined.name)
            try:
                with combined.open("wb") as out_f:
                    for part in (z01, zp):
                        with part.open("rb") as in_f:
                            shutil.copyfileobj(in_f, out_f)
                logger.info("Combined: %.1f MB", combined.stat().st_size / 1e6)

                result = subprocess.run(
                    [_BSDTAR, "-xvf", str(combined.resolve()),
                     "-C", str(out_dir.resolve())],
                    capture_output=True, text=True,
                )
                combined.unlink(missing_ok=True)

                if result.returncode != 0:
                    logger.error("bsdtar failed for %s: %s", zp.name,
                                 result.stderr[-500:])
                    continue
                # bsdtar -v writes extracted filenames to stderr ("x filename")
                for line in result.stderr.splitlines():
                    if line.startswith("x "):
                        p = out_dir / line[2:].strip()
                        if p.exists():
                            extracted.append(p)
                if delete_zip:
                    zp.unlink(missing_ok=True)
                    z01.unlink(missing_ok=True)
                    logger.info("Removed split archive parts: %s, %s", z01.name, zp.name)
            except FileNotFoundError:
                combined.unlink(missing_ok=True)
                logger.error("'bsdtar' not found at %s — cannot extract split archive %s",
                             _BSDTAR, zp.name)
            except Exception as e:
                combined.unlink(missing_ok=True)
                logger.error("Error extracting split archive %s: %s", zp, e)
        else:
            logger.info("Extracting %s …", zp.name)
            try:
                with zipfile.ZipFile(zp) as zf:
                    for member in zf.namelist():
                        fname = Path(member).name
                        if not fname:
                            continue  # skip directory entries
                        dest = out_dir / fname
                        if dest.exists():
                            logger.debug("  skip (exists): %s", fname)
                            extracted.append(dest)
                            continue
                        dest.write_bytes(zf.read(member))
                        extracted.append(dest)
                        logger.debug("  → %s", fname)
                if delete_zip:
                    zp.unlink()
                    logger.info("Removed zip: %s", zp.name)
            except zipfile.BadZipFile as e:
                logger.error("Bad zip %s: %s", zp, e)
            except Exception as e:
                logger.error("Error extracting %s: %s", zp, e)

    logger.info("Extracted %d file(s) from %d zip(s)", len(extracted), len(zip_files))
    return extracted


def organize_continuous(
    mseed_dir: Path,
    out_root: Path,
    move: bool = False,
) -> list[Path]:
    """Organize miniSEED files into out_root/YYYY/STA/ tree.

    Filename format expected: NET.STA.CHA.YYYY.DDD[.HH.MM.SS]
    e.g. KS.ADOA.HGZ.2026.125.00.00.00 → out_root/2026/ADOA/KS.ADOA.HGZ.2026.125.00.00.00

    Parameters
    ----------
    mseed_dir : flat directory of extracted miniSEED files
    out_root  : output root, e.g. Path("/home/msseo/works/Claude/data/necis/continuous")
    move      : if True, move rather than copy (saves disk space)

    Returns
    -------
    List of organized destination paths.
    """
    organized: list[Path] = []

    for src in sorted(mseed_dir.iterdir()):
        if src.is_dir():
            continue

        parts = src.name.split(".")
        m = _SEED_PAT.match(src.name)

        year: Optional[int] = int(m.group("year")) if m else None
        station: Optional[str] = parts[1] if len(parts) >= 2 else None

        if year is None or station is None:
            logger.warning("Cannot parse year/station from '%s' — skipping", src.name)
            continue

        dest_dir = out_root / str(year) / station
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name

        if dest.exists():
            logger.debug("Skip (exists): %s", dest)
            organized.append(dest)
            continue

        if move:
            shutil.move(str(src), dest)
        else:
            shutil.copy2(src, dest)

        logger.debug("%s → %s", src.name, dest)
        organized.append(dest)

    return organized


def organize_events(
    mseed_dir: Path,
    out_root: Path,
    event_id: str,
    move: bool = False,
) -> list[Path]:
    """Organize event waveform files into out_root/event_id/.

    Parameters
    ----------
    mseed_dir : flat directory of extracted files for one event
    out_root  : output root, e.g. Path("/data/events")
    event_id  : KMA event ID (used as subdirectory name)
    move      : move rather than copy

    Returns
    -------
    List of organized destination paths.
    """
    dest_dir = out_root / event_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    organized: list[Path] = []

    for src in sorted(mseed_dir.iterdir()):
        if src.is_dir():
            continue
        dest = dest_dir / src.name
        if dest.exists():
            organized.append(dest)
            continue
        if move:
            shutil.move(str(src), dest)
        else:
            shutil.copy2(src, dest)
        organized.append(dest)

    return organized


def _convert_mseed_to_sac(mseed_dir: Path, sac_dir: Path) -> None:
    """Run mseed2sac on KS.* miniSEED files, then sort .SAC files by channel band.

    Band subdirectories match the kma_waveforms convention: HG/, BG/, LG/, HH/, etc.
    The band is taken from the first two characters of the channel code (field [3]).
    """
    sac_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [_MSEED2SAC, "-i", "KS.*"],
        cwd=str(mseed_dir),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error("mseed2sac failed (exit %d): %s",
                     result.returncode, result.stderr[-400:])
    # Move *.SAC files produced in mseed_dir into sac_dir/{band}/
    moved = 0
    for sac_file in list(mseed_dir.glob("*.SAC")):
        parts = sac_file.name.split(".")
        # Filename: NET.STA..CHA.D.YYYY.DDD.HHMMSS.SAC  (parts[3] = channel)
        band = parts[3][:2] if len(parts) >= 4 else "XX"
        band_dir = sac_dir / band
        band_dir.mkdir(exist_ok=True)
        shutil.move(str(sac_file), band_dir / sac_file.name)
        moved += 1
    logger.info("Converted %d miniSEED → SAC in %s", moved, sac_dir)


def organize_events_kma(
    zip_dir: Path,
    event_utc_str: str,
    necis_id: str,
    out_root: Path,
    data_type: str = "a",
    delete_zip: bool = True,
    convert_sac: bool = True,
) -> list[Path]:
    """Extract a downloaded event ZIP and organize into the kma_waveforms layout.

    Layout produced:
      out_root/{event_utc_str}/{necis_id}.{data_type}/MSEED/  ← raw miniSEED
      out_root/{event_utc_str}/{necis_id}.{data_type}/SAC/{band}/  ← SAC files

    Parameters
    ----------
    zip_dir       : directory containing the downloaded ZIP(s) to extract
    event_utc_str : UTC origin time string, e.g. '20230526113954' (outer dir)
    necis_id      : NECIS internal event ID, e.g. '2023002939' (inner dir prefix)
    out_root      : organized output root
    data_type     : 'a' (acceleration) or 'v' (velocity)
    delete_zip    : remove ZIP after successful extraction
    convert_sac   : run mseed2sac to produce SAC files
    """
    mseed_dir = out_root / event_utc_str / f"{necis_id}.{data_type}" / "MSEED"
    sac_dir   = out_root / event_utc_str / f"{necis_id}.{data_type}" / "SAC"
    mseed_dir.mkdir(parents=True, exist_ok=True)

    extracted = extract_zips(zip_dir, mseed_dir, delete_zip=delete_zip)
    if not extracted:
        logger.warning("No files extracted for event %s type=%s", event_utc_str, data_type)
        return []

    if convert_sac:
        if shutil.which(_MSEED2SAC) or Path(_MSEED2SAC).exists():
            _convert_mseed_to_sac(mseed_dir, sac_dir)
        else:
            logger.error("mseed2sac not found at %s — skipping SAC conversion", _MSEED2SAC)

    return extracted


def process_continuous_downloads(
    zip_dir: Path,
    out_root: Path,
    move: bool = True,
    delete_zip: bool = False,
) -> list[Path]:
    """Full pipeline: extract zips then organize into out_root/YYYY/MM/DD/.

    Used by download_continuous.py after the fetch step.
    """
    staging = zip_dir / "_staging"
    extracted = extract_zips(zip_dir, staging, delete_zip=delete_zip)
    if not extracted:
        logger.info("No files extracted — nothing to organize.")
        return []

    organized = organize_continuous(staging, out_root, move=move)

    if move and staging.exists():
        try:
            staging.rmdir()
        except OSError:
            pass

    logger.info("Organized %d miniSEED file(s) into %s", len(organized), out_root)
    return organized
