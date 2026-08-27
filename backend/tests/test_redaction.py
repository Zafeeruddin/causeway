"""Credentials must not survive a trip through a log line or an f-string."""

from app.security.redaction import StreamUrl, redact


def test_credentials_never_appear_in_str_or_repr():
    url = StreamUrl.build("rtsp://10.0.0.42:554/Streaming/Channels/101", "admin", "hunter2")
    assert "hunter2" not in str(url)
    assert "hunter2" not in repr(url)
    assert "hunter2" not in f"connecting to {url}"
    assert "admin" not in str(url)
    assert str(url) == "rtsp://10.0.0.42:554/Streaming/Channels/101"


def test_credentials_embedded_in_the_url_are_stripped_out():
    url = StreamUrl.build("rtsp://admin:hunter2@10.0.0.42:554/stream1")
    assert "hunter2" not in str(url)
    assert url.has_credentials
    assert url.expose() == "rtsp://admin:hunter2@10.0.0.42:554/stream1"


def test_expose_returns_a_usable_url():
    url = StreamUrl.build("rtsp://10.0.0.42:554/s", "admin", "p@ss/word")
    # Reserved characters have to survive being put back into an authority.
    assert url.expose() == "rtsp://admin:p%40ss%2Fword@10.0.0.42:554/s"


def test_with_host_repoints_at_a_forward_and_keeps_credentials():
    url = StreamUrl.build("rtsp://10.0.0.42:554/Streaming/Channels/101", "admin", "hunter2")
    local = url.with_host("127.0.0.1", 20001)
    assert str(local) == "rtsp://127.0.0.1:20001/Streaming/Channels/101"
    assert local.expose().startswith("rtsp://admin:hunter2@127.0.0.1:20001")
    assert "hunter2" not in str(local)


def test_redact_is_a_backstop_for_free_text():
    line = "ffmpeg -i rtsp://admin:hunter2@10.0.0.42:554/s failed"
    assert "hunter2" not in redact(line)


def test_urls_without_credentials_are_unchanged():
    url = StreamUrl.build("https://cdn.example/stream/index.m3u8")
    assert not url.has_credentials
    assert url.expose() == str(url) == "https://cdn.example/stream/index.m3u8"
