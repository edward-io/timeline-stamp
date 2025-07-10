# Timeline-Stamp

Stamp JPEG photos with GPS coordinates and **correct local timestamps** using your Google Maps `Timeline.json` export.

Why?  
Digital cameras often stay on a single timezone (e.g. your home). When you travel, photo **timestamps are wrong** and lack location data. That breaks Google Photos search and map view. This tool:

1. Streams your huge `Timeline.json` (no 3rd-party servers, memory-safe)
2. Matches each photo to the nearest timeline point (default ≤ 60 min)
3. Determines the correct local timezone at that coordinate
4. Writes GPS tags and converts the EXIF `DateTime*` fields into local time (adding `OffsetTime*` tags so Google Photos understands). If the photo already has GPS, it is **skipped by default** to keep the script idempotent (use `--overwrite-gps` to override).

---

## Installation

### From source (editable)

```bash
# Clone then install with modern PEP 517 build:
pip install -e .
```

---

## Usage (dry-run by default)

First, get get your Timeline.json file from on your phone (its easiest to copy to Google Drive, then download):

`android > settings > location > location services > timeline > export timeline data`

To run:

```bash
timeline-stamp --timeline /path/Timeline.json --photos /path/to/jpegs [options]
```

Required flags:

* `--timeline`  Path to your Google Maps `Timeline.json`
* `--photos`    Directory containing `.jpg` / `.jpeg` files (recurses)

Optional flags:

| Flag | Default | Description |
|---|---|---|
| `--camera-tz` | `America/Los_Angeles` | Timezone your camera clock was set to when shooting |
| `--max-gap-minutes` | `60` | Maximum allowed time difference between photo and timeline point |
| `--apply` | *off* | Actually modify files. Without this, the script only prints what **would** change |
| `--backup` | *off* | When used *together with* `--apply`, create `photo.jpg.exif_backup` before writing |
| `--overwrite-gps` | *off* | Force update even if the photo already contains GPS tags |
| `--verbose` | *off* | Debug-level logging |

Examples:

Dry-run (safe):
```bash
timeline-stamp \
  --timeline ~/Downloads/Timeline.json \
  --photos   ~/Pictures/TripR5 \
  --camera-tz America/Los_Angeles         # camera set to PST
```

Apply changes, with backups:
```bash
timeline-stamp \
  --timeline ~/Downloads/Timeline.json \
  --photos   ~/Pictures/TripR5 \
  --apply --backup --verbose
```

---

## How it works

1. `ijson` streams `semanticSegments.*` so the timeline file never loads fully.
2. `timelinePath` points and `visit` midpoints become `(UTC time, lat, lon)` records sorted for binary search.
3. For each photo:  
   * EXIF `DateTimeOriginal` is assumed to be in `--camera-tz`.
   * The closest timeline record ≤ `--max-gap-minutes` is selected.
   * `timezonefinder` maps lat/lon -> IANA TZ, then `pytz` converts time.
   * GPS + local time + `OffsetTime*` EXIF tags are written (if `--apply`).

---

### Safety features

* **Dry-run default** – no file writes unless you pass `--apply`.
* Optional `.exif_backup` copies preserve originals.
* Uses lossless EXIF write (`piexif.insert`).

---

## Limitations / TODO

* Only JPEG supported (RAW requires different libraries).
* Matching purely by timestamp; won't fix photos without nearby timeline data.
* Google Timeline sometimes has gaps or wrong coords.

PRs welcome! 