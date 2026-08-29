"""Credentials must not survive a trip through a log line or an f-string."""

from urllib.parse import unquote, urlsplit

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


def test_an_encoded_password_in_the_url_is_not_encoded_twice():
    """urlsplit hands back userinfo still percent-encoded, and expose() quotes
    what it is given. Keeping the raw form encodes it twice, and the camera
    refuses credentials that were correct."""
    url = StreamUrl.build("rtsp://admin:CTC2.5%2B%2B@10.244.116.70:554/Streaming/Channels/101")

    assert url.expose() == "rtsp://admin:CTC2.5%2B%2B@10.244.116.70:554/Streaming/Channels/101"
    # What actually reaches the camera, once the transport decodes it again.
    assert unquote(urlsplit(url.expose()).password or "") == "CTC2.5++"


def test_an_encoded_username_survives_the_same_trip():
    url = StreamUrl.build("rtsp://user%40site:pw@10.0.0.42:554/s")
    assert unquote(urlsplit(url.expose()).username or "") == "user@site"


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
