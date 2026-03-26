import copy
import datetime as dt
from pathlib import Path
from unittest import mock

import pytz

import timeline_stamp as ts


def make_exif(*, dt_original=b"2024:12:15 06:20:15", offset_original=None, gps=False):
    exif = {
        "0th": {ts.piexif.ImageIFD.DateTime: dt_original},
        "Exif": {
            ts.piexif.ExifIFD.DateTimeOriginal: dt_original,
            ts.piexif.ExifIFD.DateTimeDigitized: dt_original,
        },
        "GPS": {},
        "1st": {},
        "thumbnail": None,
    }
    if offset_original is not None:
        exif["Exif"][ts.piexif.ExifIFD.OffsetTimeOriginal] = offset_original
        exif["Exif"][ts.piexif.ExifIFD.OffsetTimeDigitized] = offset_original
        exif["Exif"][ts.piexif.ExifIFD.OffsetTime] = offset_original
    if gps:
        exif["GPS"][ts.piexif.GPSIFD.GPSLatitude] = [(1, 1), (2, 1), (3, 1)]
        exif["GPS"][ts.piexif.GPSIFD.GPSLongitude] = [(1, 1), (2, 1), (3, 1)]
    return exif


def test_find_photo_paths_matches_jpegs_case_insensitively(tmp_path):
    for name in ["a.jpg", "b.jpeg", "c.JPG", "d.JPEG", "e.jpgg"]:
        (tmp_path / name).write_bytes(b"x")

    matched = [path.name for path in ts.find_photo_paths(tmp_path)]

    assert matched == ["a.jpg", "b.jpeg", "c.JPG", "d.JPEG"]


def test_main_counts_update_photo_false_as_skipped(tmp_path):
    photo = tmp_path / "image.jpg"
    photo.write_bytes(b"x")
    point = ts.TimelinePoint(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc), 1.0, 2.0)
    exif = make_exif(dt_original=b"2023:12:31 16:00:00")

    with mock.patch.object(ts, "load_timeline_points", return_value=[point]), \
         mock.patch.object(ts.piexif, "load", return_value=exif), \
         mock.patch.object(ts, "find_nearest_timeline_point", return_value=point), \
         mock.patch.object(ts, "update_photo", return_value=False):
        with mock.patch.object(ts.sys, "exit", side_effect=AssertionError("unexpected exit")):
            with mock.patch.object(ts.LOGGER, "info") as info_log:
                ts.main(["--timeline", "/tmp/Timeline.json", "--photos", str(tmp_path)])

    assert info_log.call_args_list[-1].args == (
        "Dry-run complete. %s photos WOULD be updated, %s skipped.",
        0,
        1,
    )


def test_update_photo_preserves_absolute_time_when_offset_tags_exist():
    point = ts.TimelinePoint(
        dt.datetime(2024, 12, 15, 14, 20, 15, tzinfo=dt.timezone.utc),
        13.7563,
        100.5018,
    )
    exif = make_exif(dt_original=b"2024:12:15 21:20:15", offset_original=b"+07:00")
    captured = {}

    def fake_dump(exif_dict):
        captured["exif"] = copy.deepcopy(exif_dict)
        return b"exif-bytes"

    class FakeTF:
        def timezone_at(self, *, lat, lng):
            return "Asia/Bangkok"

    with mock.patch.object(ts.piexif, "load", return_value=copy.deepcopy(exif)), \
         mock.patch.object(ts.piexif, "dump", side_effect=fake_dump), \
         mock.patch.object(ts.piexif, "insert"), \
         mock.patch.object(ts, "tf", FakeTF()):
        result = ts.update_photo(
            Path("/tmp/image.jpg"),
            point,
            pytz.timezone("America/Los_Angeles"),
            apply=True,
            overwrite_gps=True,
        )

    assert result is True
    assert captured["exif"]["Exif"][ts.piexif.ExifIFD.DateTimeOriginal] == b"2024:12:15 21:20:15"
    assert captured["exif"]["Exif"][ts.piexif.ExifIFD.OffsetTimeOriginal] == b"+07:00"


def test_main_uses_existing_offset_for_timeline_matching(tmp_path):
    photo = tmp_path / "image.jpg"
    photo.write_bytes(b"x")
    point = ts.TimelinePoint(
        dt.datetime(2024, 12, 15, 14, 20, 15, tzinfo=dt.timezone.utc),
        13.7563,
        100.5018,
    )
    exif = make_exif(dt_original=b"2024:12:15 21:20:15", offset_original=b"+07:00")

    with mock.patch.object(ts, "load_timeline_points", return_value=[point]), \
         mock.patch.object(ts.piexif, "load", return_value=exif), \
         mock.patch.object(ts, "update_photo", return_value=True) as update_photo:
        with mock.patch.object(ts.sys, "exit", side_effect=AssertionError("unexpected exit")):
            ts.main(
                [
                    "--timeline",
                    "/tmp/Timeline.json",
                    "--photos",
                    str(tmp_path),
                    "--camera-tz",
                    "America/Los_Angeles",
                ]
            )

    update_photo.assert_called_once()


def test_main_disambiguates_fall_back_time_using_timeline(tmp_path):
    photo = tmp_path / "image.jpg"
    photo.write_bytes(b"x")
    point = ts.TimelinePoint(
        dt.datetime(2024, 11, 3, 8, 30, tzinfo=dt.timezone.utc),
        34.0522,
        -118.2437,
    )
    exif = make_exif(dt_original=b"2024:11:03 01:30:00")

    with mock.patch.object(ts, "load_timeline_points", return_value=[point]), \
         mock.patch.object(ts.piexif, "load", return_value=exif), \
         mock.patch.object(ts, "update_photo", return_value=True) as update_photo:
        with mock.patch.object(ts.sys, "exit", side_effect=AssertionError("unexpected exit")):
            ts.main(
                [
                    "--timeline",
                    "/tmp/Timeline.json",
                    "--photos",
                    str(tmp_path),
                    "--camera-tz",
                    "America/Los_Angeles",
                ]
            )

    assert update_photo.call_args.kwargs["photo_dt_utc"] == dt.datetime(
        2024, 11, 3, 8, 30, tzinfo=dt.timezone.utc
    )


def test_main_skips_nonexistent_spring_forward_time(tmp_path):
    photo = tmp_path / "image.jpg"
    photo.write_bytes(b"x")
    point = ts.TimelinePoint(
        dt.datetime(2024, 3, 10, 10, 30, tzinfo=dt.timezone.utc),
        34.0522,
        -118.2437,
    )
    exif = make_exif(dt_original=b"2024:03:10 02:30:00")

    with mock.patch.object(ts, "load_timeline_points", return_value=[point]), \
         mock.patch.object(ts.piexif, "load", return_value=exif), \
         mock.patch.object(ts, "update_photo", return_value=True) as update_photo, \
         mock.patch.object(ts.LOGGER, "warning") as warning_log, \
         mock.patch.object(ts.LOGGER, "info") as info_log:
        with mock.patch.object(ts.sys, "exit", side_effect=AssertionError("unexpected exit")):
            ts.main(
                [
                    "--timeline",
                    "/tmp/Timeline.json",
                    "--photos",
                    str(tmp_path),
                    "--camera-tz",
                    "America/Los_Angeles",
                ]
            )

    update_photo.assert_not_called()
    assert any(
        "does not exist in camera timezone" in str(call.args[2])
        for call in warning_log.call_args_list
    )
    assert info_log.call_args_list[-1].args == (
        "Dry-run complete. %s photos WOULD be updated, %s skipped.",
        0,
        1,
    )
