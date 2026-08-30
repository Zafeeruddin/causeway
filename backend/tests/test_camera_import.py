"""Import has to be forgiving about format and strict about what it accepts,
and it has to say what it did rather than reporting a bare count."""

from __future__ import annotations

import pytest

from app.enums import SourceKind
from app.security.redaction import StreamUrl
from app.services.camera_import import (
    InvalidStreamUrl,
    parse_csv,
    parse_pasted,
    parse_source_url,
)

# ---- url parsing -------------------------------------------------------


def test_credentials_are_split_out_of_the_url():
    """The database must never hold a password inside a URL column."""
    source = parse_source_url("rtsp://admin:hunter2@10.0.0.42:554/Streaming/Channels/101")
    assert source.url == "rtsp://10.0.0.42:554/Streaming/Channels/101"
    assert "hunter2" not in source.url
    assert source.username == "admin"
    assert source.password == "hunter2"


def test_an_escaped_password_is_stored_as_the_one_that_was_typed():
    """An NVR exports `CTC2.5++` as `CTC2.5%2B%2B`. Stored escaped, it is quoted
    again on the way to the camera and the camera answers 401 -- a credentials
    error for credentials that were right."""
    source = parse_source_url("rtsp://admin:CTC2.5%2B%2B@10.244.116.70:554/Streaming/Channels/101")

    assert source.password == "CTC2.5++"
    assert StreamUrl.build(source.url, source.username, source.password).expose() == (
        "rtsp://admin:CTC2.5%2B%2B@10.244.116.70:554/Streaming/Channels/101"
    )


def test_an_escaped_username_survives_the_same_trip():
    source = parse_source_url("rtsp://user%40site:pw@10.0.0.42:554/s")
    assert source.username == "user@site"


def test_an_id_and_a_name_are_two_different_things():
    """`id` used to be an alias for `name`, which is how every imported camera
    ended up called after its own address."""
    report = parse_pasted("CAM-14, North gate, rtsp://10.0.0.42:554/s1")

    camera = report.cameras[0]
    assert camera.ref == "CAM-14"
    assert camera.name == "North gate"


def test_one_field_before_the_url_is_still_a_name():
    """Every list pasted before today had this shape; it has to keep working."""
    report = parse_pasted("North gate, rtsp://10.0.0.42:554/s1")
    assert report.cameras[0].name == "North gate"
    assert report.cameras[0].ref == ""


def test_a_bare_url_gets_a_name_worth_reading():
    """An address alone is a poor name: a list of them is unreadable and none of
    them says which door it points at."""
    report = parse_pasted("CAM-14,,rtsp://10.0.0.42:554/Streaming/Channels/101")
    assert report.cameras[0].ref == "CAM-14"

    plain = parse_pasted("rtsp://10.0.0.42:554/s1").cameras[0]
    assert plain.name == "10.0.0.42 · s1"


def test_a_csv_keeps_the_id_column_out_of_the_name():
    report = parse_csv(
        "id,name,rtsp_url\n"
        "CAM-14,North gate,rtsp://10.0.0.42:554/s1\n"
        "CAM-15,,rtsp://10.0.0.43:554/s1\n"
    )
    first, second = report.cameras
    assert (first.ref, first.name) == ("CAM-14", "North gate")
    # No name given, so one is built -- and it leads with the identifier the
    # customer actually uses rather than the address.
    assert second.ref == "CAM-15"
    assert second.name.startswith("CAM-15 · 10.0.0.43")


def test_the_default_rtsp_port_is_filled_in():
    assert parse_source_url("rtsp://10.0.0.42/stream1").port == 554


def test_query_strings_survive():
    source = parse_source_url("rtsp://10.0.0.42:554/live?channel=2&subtype=1")
    assert source.url.endswith("/live?channel=2&subtype=1")


def test_an_hls_url_is_recognised_as_a_different_kind():
    source = parse_source_url("https://cdn.example.com/live/cam1/index.m3u8")
    assert source.kind is SourceKind.HLS
    assert source.port == 443


def test_an_https_url_that_is_not_a_playlist_is_rejected():
    with pytest.raises(InvalidStreamUrl, match="m3u8"):
        parse_source_url("https://cdn.example.com/live/cam1")


def test_a_non_stream_scheme_says_what_was_expected():
    with pytest.raises(InvalidStreamUrl, match="expected rtsp"):
        parse_source_url("ftp://10.0.0.42/stream")


# ---- pasted blocks -----------------------------------------------------


def test_a_pasted_block_of_bare_urls():
    report = parse_pasted(
        """
        rtsp://10.0.0.41:554/Streaming/Channels/101
        rtsp://10.0.0.42:554/Streaming/Channels/101
        rtsp://10.0.0.43:554/Streaming/Channels/101
        """
    )
    assert len(report.cameras) == 3
    assert not report.rejected


def test_a_leading_name_is_honoured():
    report = parse_pasted("Gate camera, rtsp://10.0.0.41:554/s1\nrtsp://10.0.0.42:554/s1")
    assert report.cameras[0].name == "Gate camera"
    # The unnamed one still gets something recognisable rather than "camera 2".
    assert "10.0.0.42" in report.cameras[1].name


def test_duplicates_are_reported_not_silently_dropped():
    report = parse_pasted(
        "rtsp://10.0.0.41:554/s1\nrtsp://10.0.0.41:554/s1\nrtsp://admin:pw@10.0.0.41:554/s1\n"
    )
    assert len(report.cameras) == 1
    assert len(report.duplicates) == 2, "same stream with different credentials is still the same"
    assert "line 1" in report.duplicates[0].reason


def test_bad_lines_are_reported_with_their_line_number():
    report = parse_pasted("rtsp://10.0.0.41:554/s1\nnot a url\nhttp://example.com/page")
    assert len(report.cameras) == 1
    assert len(report.rejected) == 2
    assert report.rejected[0].line == 2


def test_comments_and_blank_lines_are_ignored():
    report = parse_pasted("# west wing\n\nrtsp://10.0.0.41:554/s1\n\n")
    assert len(report.cameras) == 1
    assert not report.rejected


def test_the_summary_names_every_outcome():
    report = parse_pasted("rtsp://10.0.0.41:554/s1\nrtsp://10.0.0.41:554/s1\nbroken")
    assert report.summary == "1 ready, 1 duplicate, 1 rejected"


# ---- csv ---------------------------------------------------------------


CSV = """name,location,rtsp_url,hls_url,username,password
Gate,North,rtsp://10.0.0.41:554/s1,https://cdn.example.com/live/gate/index.m3u8,admin,hunter2
Lobby,South,rtsp://10.0.0.42:554/s1,,admin,hunter2
"""


def test_csv_import_reads_both_sources_per_camera():
    report = parse_csv(CSV)
    assert len(report.cameras) == 2
    gate = report.cameras[0]
    assert gate.name == "Gate"
    assert gate.location == "North"
    assert {s.kind for s in gate.sources} == {SourceKind.RTSP, SourceKind.HLS}
    assert len(report.cameras[1].sources) == 1


def test_row_credentials_apply_to_sources_that_have_none():
    gate = parse_csv(CSV).cameras[0]
    rtsp = next(s for s in gate.sources if s.kind is SourceKind.RTSP)
    assert rtsp.username == "admin"
    assert rtsp.password == "hunter2"
    assert "hunter2" not in rtsp.url


def test_column_names_are_matched_loosely():
    """People export from different tools; the header is never quite the same."""
    report = parse_csv("Camera Name;RTSP URL;Site\nGate;rtsp://10.0.0.41:554/s1;North\n")
    assert report.cameras[0].name == "Gate"
    assert report.cameras[0].location == "North"


def test_tab_separated_files_work_too():
    report = parse_csv("name\trtsp\nGate\trtsp://10.0.0.41:554/s1\n")
    assert len(report.cameras) == 1


def test_a_utf8_bom_does_not_break_the_header():
    """Excel writes one, and it turns the first column name into garbage."""
    report = parse_csv("﻿name,rtsp_url\nGate,rtsp://10.0.0.41:554/s1\n")
    assert report.cameras[0].name == "Gate"


def test_a_file_with_no_url_column_says_which_column_to_add():
    report = parse_csv("name,location\nGate,North\n")
    assert not report.cameras
    assert "rtsp_url" in report.rejected[0].reason


def test_a_bad_url_in_one_row_does_not_lose_the_others():
    report = parse_csv("name,rtsp_url\nGate,rtsp://10.0.0.41:554/s1\nBroken,nonsense\n")
    assert len(report.cameras) == 1
    assert report.rejected[0].line == 3


def test_an_hls_url_in_the_rtsp_column_is_rejected_clearly():
    report = parse_csv("name,rtsp_url\nGate,https://cdn.example.com/x/index.m3u8\n")
    assert not report.cameras
    assert "not a rtsp URL" in report.rejected[0].reason
