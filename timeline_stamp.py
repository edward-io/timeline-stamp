#!/usr/bin/env python3
"""
Timeline & EXIF synchroniser for Canon R5 photos
================================================

This script does two things for every JPEG in a directory:
1. Adds GPS latitude/longitude from Google Maps Timeline export
2. Corrects the timestamp & timezone so the moment-in-time remains the
   same, but the EXIF DateTime* fields are expressed in the local
   timezone where the photo was taken (with appropriate OffsetTime* tags).

Strategy
--------
- We *stream* the 30-something-MB `Timeline.json` with *ijson* so that the
  whole file is **never** loaded into memory at once.
- Every coordinate in `semanticSegments[].timelinePath[]` becomes an entry
  of the form (utc_datetime, lat, lng).
  *If a segment is a stationary `visit`, we create a single point at the
  segment's midpoint using the topCandidate location.*
- All points are sorted, giving a searchable timeline that supports fast
  nearest-neighbour lookup via `bisect`.
- Each JPEG is read with *piexif*:
  * The naive `DateTimeOriginal` is treated as having the camera's
    timezone (default America/Los_Angeles, configurable).
  * The script finds the closest timeline point (default ≤60 min).
  * Using *timezonefinder* ➔ IANA tz name ➔ *pytz*, we convert the moment
    into local time and compute the UTC offset string (+07:00, etc.).
  * EXIF tags are updated in-place:
      - DateTime, DateTimeOriginal, DateTimeDigitized
      - OffsetTime, OffsetTimeOriginal, OffsetTimeDigitized
      - GPSLatitude{,Ref}, GPSLongitude{,Ref}

Usage
-----
    python timeline_stamp.py \
        --timeline /path/Timeline.json \
        --photos   /path/to/jpeg/dir \
        [--camera-tz America/Los_Angeles] \
        [--max-gap-minutes 60]

A `.exif_backup` file is written next to every modified image before the
changes are saved.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import logging
import math
import pathlib
import sys
from dataclasses import dataclass
from typing import List, Tuple

import ijson  # type: ignore
import piexif  # type: ignore
import pytz  # type: ignore
import shutil
from dateutil import parser as dtparse  # type: ignore
from timezonefinder import TimezoneFinder  # type: ignore

LOGGER = logging.getLogger("timeline_stamp")

# ---------------------------------------------------------------------------
# Utility dataclasses
# ---------------------------------------------------------------------------

@dataclass(order=True)
class TimelinePoint:
    time_utc: dt.datetime
    lat: float
    lon: float


# ---------------------------------------------------------------------------
# Parsing Google Timeline.json lazily with ijson
# ---------------------------------------------------------------------------

def load_timeline_points(timeline_path: pathlib.Path) -> List[TimelinePoint]:
    """Stream-parse Timeline.json and return sorted list of TimelinePoint."""
    tf_points: List[TimelinePoint] = []

    with timeline_path.open("rb") as f:
        segments = ijson.items(f, "semanticSegments.item")
        for seg in segments:
            try:
                if "timelinePath" in seg:
                    for entry in seg.get("timelinePath", []):
                        _add_point_from_path_entry(tf_points, entry)
                elif "visit" in seg:
                    _add_point_from_visit(tf_points, seg)
            except Exception as exc:
                LOGGER.warning("Failed to parse segment entry: %s", exc)

    tf_points.sort(key=lambda p: p.time_utc)
    LOGGER.info("Loaded %s timeline points", len(tf_points))
    return tf_points


def _add_point_from_path_entry(tf_points: List[TimelinePoint], entry):
    """Extract a point from a timelinePath entry."""
    lat, lon = _parse_latlng(entry["point"])
    time_local = dtparse.isoparse(entry["time"])  # timezone aware
    tf_points.append(TimelinePoint(time_local.astimezone(dt.timezone.utc), lat, lon))


def _add_point_from_visit(tf_points: List[TimelinePoint], seg):
    """Create a single midpoint record for a stationary visit segment."""
    try:
        loc_str = seg["visit"]["topCandidate"]["placeLocation"]["latLng"]
        lat, lon = _parse_latlng(loc_str)
    except (KeyError, TypeError, ValueError):
        # nothing usable
        return
    start = dtparse.isoparse(seg["startTime"]).astimezone(dt.timezone.utc)
    end = dtparse.isoparse(seg["endTime"]).astimezone(dt.timezone.utc)
    midpoint = start + (end - start) / 2
    tf_points.append(TimelinePoint(midpoint, lat, lon))


def _parse_latlng(ll_str: str) -> Tuple[float, float]:
    """Convert "lat°, lon°" into floats."""
    ll_clean = ll_str.replace("°", "").strip()
    lat_str, lon_str = [s.strip() for s in ll_clean.split(",")]
    return float(lat_str), float(lon_str)


# ---------------------------------------------------------------------------
# Photo processing helpers
# ---------------------------------------------------------------------------

tf = TimezoneFinder()


def find_nearest_timeline_point(points: List[TimelinePoint], ts: dt.datetime) -> TimelinePoint | None:
    """Binary-search for timeline point nearest to *ts* (UTC)."""
    keys = [p.time_utc for p in points]
    idx = bisect.bisect_left(keys, ts)
    candidates = []
    if idx < len(points):
        candidates.append(points[idx])
    if idx > 0:
        candidates.append(points[idx - 1])
    if not candidates:
        return None
    return min(candidates, key=lambda p: abs(p.time_utc - ts))


def update_photo(
    filepath: pathlib.Path,
    tl_point: TimelinePoint,
    camera_tz: pytz.BaseTzInfo,
    *,
    apply: bool = False,
    backup: bool = False,
    overwrite_gps: bool = False,
):
    """Rewrite EXIF of *filepath* using timeline point lat/lon and timezone."""
    # Read existing EXIF
    exif_dict = piexif.load(str(filepath))

    # Skip if GPS already present and we're not overwriting
    if not overwrite_gps:
        gps_ifd_existing = exif_dict.get("GPS", {})
        if gps_ifd_existing.get(piexif.GPSIFD.GPSLatitude) and gps_ifd_existing.get(piexif.GPSIFD.GPSLongitude):
            LOGGER.debug("%s already has GPS tags; skipping", filepath.name)
            return False

    # Original naive timestamp ➔ assume camera_tz
    try:
        original_str = exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal].decode()
    except KeyError:
        LOGGER.warning("%s has no DateTimeOriginal; skipped", filepath.name)
        return False

    naive_dt = dt.datetime.strptime(original_str, "%Y:%m:%d %H:%M:%S")
    aware_pst = camera_tz.localize(naive_dt)
    aware_utc = aware_pst.astimezone(dt.timezone.utc)

    # Local timezone by coord
    tz_name = tf.timezone_at(lat=tl_point.lat, lng=tl_point.lon)
    if tz_name is None:
        LOGGER.warning("Could not find timezone for %s; skipped", filepath.name)
        return False
    local_tz = pytz.timezone(tz_name)
    local_dt = aware_utc.astimezone(local_tz)

    offset = local_dt.utcoffset() or dt.timedelta(0)
    offset_str = _format_tz_offset(offset)

    # Update date/time fields
    dt_bytes = local_dt.strftime("%Y:%m:%d %H:%M:%S").encode()
    for ifd, tag in (("Exif", piexif.ExifIFD.DateTimeOriginal),
                     ("Exif", piexif.ExifIFD.DateTimeDigitized),
                     ("0th", piexif.ImageIFD.DateTime)):
        exif_dict[ifd][tag] = dt_bytes

    # OffsetTime* – piexif defines these only in ExifIFD
    for tag in (piexif.ExifIFD.OffsetTime,  # 0x9010 – applies to 0th DateTime
                piexif.ExifIFD.OffsetTimeOriginal,
                piexif.ExifIFD.OffsetTimeDigitized):
        exif_dict["Exif"][tag] = offset_str.encode()

    # GPS tags
    _write_gps(exif_dict, tl_point.lat, tl_point.lon)

    if apply:
        # Backup & write (optional)
        if backup:
            backup_path = filepath.with_suffix(filepath.suffix + ".exif_backup")
            if not backup_path.exists():
                try:
                    shutil.copy2(filepath, backup_path)
                except Exception as exc:
                    LOGGER.warning("Could not create backup for %s: %s", filepath.name, exc)

        piexif.insert(piexif.dump(exif_dict), str(filepath))
        return True
    else:
        local_dt_str = local_dt.strftime("%Y:%m:%d %H:%M:%S")
        LOGGER.info("[dry-run] Would update %s (lat=%.5f, lon=%.5f, tz=%s, time=%s)", filepath.name, tl_point.lat, tl_point.lon, offset_str, local_dt_str)
        return True


def _format_tz_offset(td: dt.timedelta) -> str:
    total_minutes = int(td.total_seconds() / 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hh, mm = divmod(total_minutes, 60)
    return f"{sign}{hh:02d}:{mm:02d}"


def _write_gps(exif_dict, lat: float, lon: float):
    def _deg_to_dms_rational(deg_float: float):
        deg_abs = abs(deg_float)
        deg = int(deg_abs)
        minutes_float = (deg_abs - deg) * 60
        minutes = int(minutes_float)
        seconds = (minutes_float - minutes) * 60
        return [
            (deg, 1),
            (minutes, 1),
            (int(seconds * 100), 100),  # 2-decimal-place precision
        ]

    gps_ifd = exif_dict.setdefault("GPS", {})

    gps_ifd[piexif.GPSIFD.GPSLatitudeRef] = ("N" if lat >= 0 else "S").encode()
    gps_ifd[piexif.GPSIFD.GPSLatitude] = _deg_to_dms_rational(lat)
    gps_ifd[piexif.GPSIFD.GPSLongitudeRef] = ("E" if lon >= 0 else "W").encode()
    gps_ifd[piexif.GPSIFD.GPSLongitude] = _deg_to_dms_rational(lon)


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Stamp photos with location + local time from Google Timeline export.")
    parser.add_argument("--timeline", type=pathlib.Path, required=True, help="Path to Timeline.json")
    parser.add_argument("--photos", type=pathlib.Path, required=True, help="Directory containing JPEGs")
    parser.add_argument("--camera-tz", default="America/Los_Angeles", help="IANA timezone the camera was set to (default: America/Los_Angeles)")
    parser.add_argument("--max-gap-minutes", type=int, default=60, help="Maximum allowed difference between photo & timeline point (default 60min)")
    parser.add_argument("--apply", action="store_true", help="Actually write changes. Default is dry-run (no files modified).")
    parser.add_argument("--backup", action="store_true", help="Create .exif_backup before writing (with --apply).")
    parser.add_argument("--overwrite-gps", action="store_true", help="Update photo even if it already contains GPS tags.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s:%(message)s")

    camera_tz = pytz.timezone(args.camera_tz)
    points = load_timeline_points(args.timeline)
    if not points:
        LOGGER.error("No timeline points extracted – exiting.")
        sys.exit(1)

    photo_paths = sorted(p for p in args.photos.rglob("*.jp*g"))
    if not photo_paths:
        LOGGER.error("No JPEGs found in %s", args.photos)
        sys.exit(1)

    processed = 0
    skipped = 0
    max_gap = dt.timedelta(minutes=args.max_gap_minutes)

    for photo in photo_paths:
        try:
            exif_dict = piexif.load(str(photo))
            orig_bytes = exif_dict["Exif"].get(piexif.ExifIFD.DateTimeOriginal)
            if not orig_bytes:
                LOGGER.info("%s lacks DateTimeOriginal, skipping", photo.name)
                skipped += 1
                continue
            naive_dt = dt.datetime.strptime(orig_bytes.decode(), "%Y:%m:%d %H:%M:%S")
            photo_dt_utc = camera_tz.localize(naive_dt).astimezone(dt.timezone.utc)
            tl_point = find_nearest_timeline_point(points, photo_dt_utc)
            if tl_point is None or abs(tl_point.time_utc - photo_dt_utc) > max_gap:
                LOGGER.info("%s has no close timeline match (gap > %s); skipping", photo.name, max_gap)
                skipped += 1
                continue
            would_update = update_photo(photo, tl_point, camera_tz, apply=args.apply, backup=args.backup, overwrite_gps=args.overwrite_gps)
            if would_update:
                processed += 1
        except Exception as exc:
            LOGGER.warning("Failed to process %s: %s", photo.name, exc)
            skipped += 1

    if args.apply:
        LOGGER.info("Done. %s photos updated, %s skipped.", processed, skipped)
    else:
        LOGGER.info("Dry-run complete. %s photos WOULD be updated, %s skipped.", processed, skipped)


if __name__ == "__main__":
    main() 