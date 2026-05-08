"""Unit tests for the YouTube Music (Free) provider."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest


# Imports below are resolved against the stubs registered in conftest.py.
from music_assistant_models.enums import (
    AlbumType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
)

import ytmusic_free as ytm


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_module_constants_present():
    assert ytm.YTM_DOMAIN == "https://music.youtube.com"
    assert ytm.VARIOUS_ARTISTS_YTM_ID == "UCUTXlgdcKU5vfzFqHOWIvkA"
    assert ytm.DEFAULT_STREAM_URL_EXPIRATION == 3600


def test_base_features_are_anonymous_safe():
    assert ProviderFeature.SEARCH in ytm.BASE_FEATURES
    assert ProviderFeature.BROWSE in ytm.BASE_FEATURES
    assert ProviderFeature.ARTIST_ALBUMS in ytm.BASE_FEATURES
    assert ProviderFeature.ARTIST_TOPTRACKS in ytm.BASE_FEATURES
    assert ProviderFeature.SIMILAR_TRACKS in ytm.BASE_FEATURES


def test_authenticated_features_separate_from_base():
    overlap = ytm.BASE_FEATURES & ytm.AUTHENTICATED_FEATURES
    assert overlap == set(), "library/auth features must not double-up with base set"
    assert ProviderFeature.LIBRARY_TRACKS in ytm.AUTHENTICATED_FEATURES
    assert ProviderFeature.RECOMMENDATIONS in ytm.AUTHENTICATED_FEATURES


def test_auth_constants():
    assert ytm.AUTH_TYPE_NONE == "none"
    assert ytm.AUTH_TYPE_COOKIE == "cookie"
    assert ytm.CONF_AUTH_TYPE == "auth_type"
    assert ytm.CONF_COOKIE == "cookie_header"


# ---------------------------------------------------------------------------
# _yt_playlist_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("playlist_id", "expected"),
    [
        ("VLPLxxx123", "https://www.youtube.com/playlist?list=PLxxx123"),
        ("PLxxx123", "https://www.youtube.com/playlist?list=PLxxx123"),
        ("OLAK5uy_abc", "https://www.youtube.com/playlist?list=OLAK5uy_abc"),
        ("VLOLAK5uy_abc", "https://www.youtube.com/playlist?list=OLAK5uy_abc"),
    ],
)
def test_yt_playlist_url_strips_vl_prefix(playlist_id, expected):
    assert ytm.YoutubeMusicFreeProvider._yt_playlist_url(playlist_id) == expected


# ---------------------------------------------------------------------------
# Cookie / auth file building
# ---------------------------------------------------------------------------


def test_build_auth_file_rejects_cookie_without_secure_3papisid(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(ytm, "open", lambda *a, **kw: pytest.fail("must not write file"), raising=False)
    with pytest.raises(ValueError, match="__Secure-3PAPISID"):
        provider._build_auth_file("SID=abc; HSID=def")


def test_build_auth_file_rejects_cookie_with_no_extractable_sapisid(provider, monkeypatch):
    # __Secure-3PAPISID present in the string but only as a substring,
    # never as its own `name=value` pair.
    monkeypatch.setattr(ytm, "open", lambda *a, **kw: pytest.fail("must not write file"), raising=False)
    with pytest.raises(ValueError, match="SAPISID"):
        provider._build_auth_file("note=__Secure-3PAPISID-mention; SID=abc")


def test_build_auth_file_extracts_sapisid_when_present(provider, tmp_path, monkeypatch):
    captured = {}

    class _DummyFile:
        def __init__(self, path):
            captured["path"] = path
            captured["buffer"] = []

        def write(self, data):
            captured["buffer"].append(data)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(path, *a, **kw):
        return _DummyFile(path)

    monkeypatch.setattr("builtins.open", _open)
    cookie = "SAPISID=mySapisid; __Secure-3PAPISID=otherValue; SID=foo"
    path = provider._build_auth_file(cookie)

    assert path == "/data/ytmusic_browser_auth.json"
    headers = json.loads("".join(captured["buffer"]))
    assert headers["cookie"] == cookie
    assert headers["origin"] == ytm.YTM_DOMAIN
    assert headers["x-origin"] == ytm.YTM_DOMAIN
    # Authorization is SAPISIDHASH <ts>_<sha1(<ts> <sapisid> <origin>)>
    assert headers["authorization"].startswith("SAPISIDHASH ")
    ts_str, hash_str = headers["authorization"][len("SAPISIDHASH "):].split("_")
    assert ts_str.isdigit()
    assert int(ts_str) <= int(time.time()) + 5
    assert len(hash_str) == 40  # sha1 hex digest


def test_build_auth_file_falls_back_to_secure_3papisid_when_sapisid_missing(
    provider, monkeypatch
):
    captured = {}

    class _DummyFile:
        def __init__(self, *_):
            captured["buffer"] = []

        def write(self, data):
            captured["buffer"].append(data)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("builtins.open", lambda *a, **kw: _DummyFile())
    cookie = "__Secure-3PAPISID=fallbackValue; SID=foo"
    provider._build_auth_file(cookie)
    headers = json.loads("".join(captured["buffer"]))
    # The hash uses the extracted SAPISID — we can't see the secret, but we can
    # confirm the same input produces a stable-shape header.
    assert headers["authorization"].startswith("SAPISIDHASH ")


# ---------------------------------------------------------------------------
# _parse_track
# ---------------------------------------------------------------------------


def test_parse_track_minimal(provider):
    track = provider._parse_track(
        {
            "videoId": "abc123",
            "title": "Some Song",
            "artists": [{"id": "UCart", "name": "An Artist"}],
        }
    )
    assert track.item_id == "abc123"
    assert track.name == "Some Song"
    assert track.artists[0].item_id == "UCart"
    assert track.artists[0].name == "An Artist"
    mappings = list(track.provider_mappings)
    assert mappings[0].item_id == "abc123"
    assert mappings[0].provider_domain == "ytmusic_free"
    assert mappings[0].url == f"{ytm.YTM_DOMAIN}/watch?v=abc123"


def test_parse_track_missing_video_id_raises(provider):
    with pytest.raises(InvalidDataError, match="videoId"):
        provider._parse_track({"title": "no id"})


def test_parse_track_missing_artists_raises(provider):
    with pytest.raises(InvalidDataError, match="artists"):
        provider._parse_track({"videoId": "abc", "title": "x", "artists": []})


def test_parse_track_artist_fallback_when_id_missing(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "Song",
            "artists": [{"name": "Solo Singer"}],
        }
    )
    assert track.artists[0].name == "Solo Singer"
    assert track.artists[0].item_id == "unknown_Solo Singer"


def test_parse_track_various_artists_resolves_to_canonical_id(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "Compilation Song",
            "artists": [{"name": "Various Artists"}],
        }
    )
    assert track.artists[0].item_id == ytm.VARIOUS_ARTISTS_YTM_ID


def test_parse_track_duration_from_seconds(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
            "duration_seconds": "245",
        }
    )
    assert track.duration == 245


def test_parse_track_duration_from_int(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
            "duration": "180",
        }
    )
    assert track.duration == 180


def test_parse_track_album_mapping(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
            "album": {"id": "MPREb_album", "name": "Album Name"},
        }
    )
    assert track.album.item_id == "MPREb_album"
    assert track.album.name == "Album Name"
    assert track.album.media_type == MediaType.ALBUM


def test_parse_track_track_number_kwarg(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
        },
        track_number=7,
    )
    assert track.track_number == 7


# ---------------------------------------------------------------------------
# _parse_album
# ---------------------------------------------------------------------------


def test_parse_album_basic(provider):
    album = provider._parse_album(
        {
            "browseId": "MPREb_xyz",
            "title": "An Album",
            "artists": [{"id": "UC1", "name": "Artist"}],
            "year": "2023",
            "type": "Album",
        }
    )
    assert album.item_id == "MPREb_xyz"
    assert album.name == "An Album"
    assert album.year == 2023
    assert album.album_type == AlbumType.ALBUM


def test_parse_album_missing_id_raises(provider):
    with pytest.raises(InvalidDataError, match="ID"):
        provider._parse_album({"title": "no id"})


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("Single", AlbumType.SINGLE),
        ("EP", AlbumType.EP),
        ("Album", AlbumType.ALBUM),
        ("", AlbumType.UNKNOWN),
        ("Compilation", AlbumType.UNKNOWN),
    ],
)
def test_parse_album_type_mapping(provider, raw_type, expected):
    album = provider._parse_album(
        {"browseId": "MPREb_x", "title": "A", "type": raw_type}
    )
    assert album.album_type == expected


def test_parse_album_explicit_id_argument_wins(provider):
    album = provider._parse_album(
        {"browseId": "ignored", "title": "A"}, album_id="explicit-id"
    )
    assert album.item_id == "explicit-id"


def test_parse_album_inferred_live(provider):
    album = provider._parse_album(
        {"browseId": "MPREb_live", "title": "Live at the Apollo", "type": "Album"}
    )
    assert album.album_type == AlbumType.LIVE


# ---------------------------------------------------------------------------
# _parse_artist
# ---------------------------------------------------------------------------


def test_parse_artist_basic(provider):
    artist = provider._parse_artist(
        {"channelId": "UCabc", "name": "An Artist"}
    )
    assert artist.item_id == "UCabc"
    assert artist.name == "An Artist"


def test_parse_artist_uses_id_field_when_channelid_missing(provider):
    artist = provider._parse_artist({"id": "UC123", "name": "Other"})
    assert artist.item_id == "UC123"


def test_parse_artist_various_artists_canonical_id(provider):
    artist = provider._parse_artist({"name": "Various Artists"})
    assert artist.item_id == ytm.VARIOUS_ARTISTS_YTM_ID


def test_parse_artist_missing_id_raises(provider):
    with pytest.raises(InvalidDataError, match="ID"):
        provider._parse_artist({"name": "Mystery"})


# ---------------------------------------------------------------------------
# _parse_playlist
# ---------------------------------------------------------------------------


def test_parse_playlist_id_field(provider):
    playlist = provider._parse_playlist({"id": "PL123", "title": "P"})
    assert playlist.item_id == "PL123"
    assert playlist.is_editable is False


def test_parse_playlist_falls_back_to_browse_id(provider):
    playlist = provider._parse_playlist({"browseId": "VLPL456", "title": "P"})
    assert playlist.item_id == "VLPL456"


def test_parse_playlist_owner_string(provider):
    playlist = provider._parse_playlist(
        {"id": "PL", "title": "P", "author": "Some User"}
    )
    assert playlist.owner == "Some User"


def test_parse_playlist_owner_list_of_dicts(provider):
    playlist = provider._parse_playlist(
        {"id": "PL", "title": "P", "author": [{"name": "First"}, {"name": "Second"}]}
    )
    assert playlist.owner == "First"


def test_parse_playlist_owner_dict(provider):
    playlist = provider._parse_playlist(
        {"id": "PL", "title": "P", "author": {"name": "Channel"}}
    )
    assert playlist.owner == "Channel"


def test_parse_playlist_owner_default_to_provider_name(provider):
    playlist = provider._parse_playlist({"id": "PL", "title": "P"})
    assert playlist.owner == provider.name


# ---------------------------------------------------------------------------
# _parse_thumbnails
# ---------------------------------------------------------------------------


def test_parse_thumbnails_picks_largest_first(provider):
    thumbs = [
        {"url": "https://example/a=w200-h200", "width": 200, "height": 200},
        {"url": "https://example/a=w800-h800", "width": 800, "height": 800},
        {"url": "https://example/a=w400-h400", "width": 400, "height": 400},
    ]
    images = provider._parse_thumbnails(thumbs)
    assert len(images) == 1
    assert "w800" in images[0].path or "w600" in images[0].path
    assert images[0].type == ImageType.THUMB


def test_parse_thumbnails_landscape_for_maxres(provider):
    thumbs = [
        {"url": "https://example/maxresdefault.jpg", "width": 1280, "height": 720},
    ]
    images = provider._parse_thumbnails(thumbs)
    assert images[0].type == ImageType.LANDSCAPE


def test_parse_thumbnails_skips_empty_url(provider):
    thumbs = [{"url": "", "width": 800, "height": 800}]
    images = provider._parse_thumbnails(thumbs)
    assert images == []


def test_parse_thumbnails_skips_low_res_without_size_param(provider):
    thumbs = [{"url": "https://example/raw.jpg", "width": 100, "height": 100}]
    images = provider._parse_thumbnails(thumbs)
    assert images == []


# ---------------------------------------------------------------------------
# _minimal_track
# ---------------------------------------------------------------------------


def test_minimal_track_returns_playable_stub(provider):
    track = provider._minimal_track("vid42")
    assert track.item_id == "vid42"
    assert track.name == "vid42"
    assert track.artists[0].name == "Unknown Artist"
    mapping = next(iter(track.provider_mappings))
    assert mapping.url == f"{ytm.YTM_DOMAIN}/watch?v=vid42"
    assert mapping.audio_format.content_type == ContentType.M4A


# ---------------------------------------------------------------------------
# get_config_entries
# ---------------------------------------------------------------------------


def test_get_config_entries_returns_expected_keys():
    entries = asyncio.run(ytm.get_config_entries(mass=None))
    keys = [e.key for e in entries]
    assert keys == [
        ytm.CONF_AUTH_TYPE,
        ytm.CONF_COOKIE,
        ytm.CONF_BRAND_ACCOUNT,
        ytm.CONF_PREFER_AUDIO_QUALITY,
    ]
    cookie_entry = next(e for e in entries if e.key == ytm.CONF_COOKIE)
    assert cookie_entry.depends_on == ytm.CONF_AUTH_TYPE
    assert cookie_entry.depends_on_value == [ytm.AUTH_TYPE_COOKIE]


# ---------------------------------------------------------------------------
# Async dispatch
# ---------------------------------------------------------------------------


def _make_ytm_search_mock(results):
    mock = MagicMock()
    mock.search = MagicMock(return_value=results)
    return mock


def test_search_artist_dispatches_with_artists_filter(provider):
    captured = {}

    def _search(query, filter, limit):
        captured["filter"] = filter
        return []

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    asyncio.run(provider.search("foo", [MediaType.ARTIST], limit=3))
    assert captured["filter"] == "artists"


def test_search_track_dispatches_with_songs_filter(provider):
    captured = {}

    def _search(query, filter, limit):
        captured["filter"] = filter
        return []

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    asyncio.run(provider.search("foo", [MediaType.TRACK], limit=3))
    assert captured["filter"] == "songs"


def test_search_multi_type_uses_no_filter(provider):
    captured = {}

    def _search(query, filter, limit):
        captured["filter"] = filter
        return []

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    asyncio.run(provider.search("foo", [MediaType.TRACK, MediaType.ALBUM], limit=3))
    assert captured["filter"] is None


def test_search_parses_returned_items_by_result_type(provider):
    mock = MagicMock()
    mock.search = MagicMock(
        return_value=[
            {
                "resultType": "artist",
                "channelId": "UCart",
                "name": "Some Artist",
            },
            {
                "resultType": "song",
                "videoId": "vid1",
                "title": "Song",
                "artists": [{"id": "UCart", "name": "Some Artist"}],
            },
            {
                "resultType": "album",
                "browseId": "MPREb_x",
                "title": "Album",
                "artists": [{"id": "UCart", "name": "Some Artist"}],
                "type": "Album",
            },
            {
                "resultType": "playlist",
                "browseId": "VLPLx",
                "title": "Playlist",
            },
        ]
    )
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search(
            "foo",
            [MediaType.ARTIST, MediaType.TRACK, MediaType.ALBUM, MediaType.PLAYLIST],
        )
    )
    assert len(results.artists) == 1
    assert len(results.tracks) == 1
    assert len(results.albums) == 1
    assert len(results.playlists) == 1


def test_search_skips_invalid_items(provider):
    """An item missing a required field should be skipped, not crash the search."""
    mock = MagicMock()
    mock.search = MagicMock(
        return_value=[
            # No videoId — should be silently skipped.
            {
                "resultType": "song",
                "title": "broken",
                "artists": [{"id": "UCart", "name": "A"}],
            },
            {
                "resultType": "song",
                "videoId": "good",
                "title": "ok",
                "artists": [{"id": "UCart", "name": "A"}],
            },
        ]
    )
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.TRACK]))
    assert len(results.tracks) == 1
    assert results.tracks[0].item_id == "good"


def test_get_album_raises_when_not_found(provider):
    mock = MagicMock()
    mock.get_album = MagicMock(return_value=None)
    provider._ytmusic = mock
    with pytest.raises(MediaNotFoundError):
        asyncio.run(provider.get_album("MPREb_missing"))


def test_get_album_tracks_returns_empty_on_none(provider):
    mock = MagicMock()
    mock.get_album = MagicMock(return_value=None)
    provider._ytmusic = mock
    tracks = asyncio.run(provider.get_album_tracks("MPREb_missing"))
    assert tracks == []


def test_get_album_tracks_assigns_track_numbers(provider):
    mock = MagicMock()
    mock.get_album = MagicMock(
        return_value={
            "tracks": [
                {
                    "videoId": "v1",
                    "title": "First",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
                {
                    "videoId": "v2",
                    "title": "Second",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
            ]
        }
    )
    provider._ytmusic = mock
    tracks = asyncio.run(provider.get_album_tracks("MPREb_x"))
    assert [t.item_id for t in tracks] == ["v1", "v2"]
    assert [t.track_number for t in tracks] == [1, 2]


def test_get_track_falls_back_to_minimal_track_on_failure(provider):
    mock = MagicMock()
    mock.get_song = MagicMock(side_effect=RuntimeError("boom"))
    provider._ytmusic = mock
    track = asyncio.run(provider.get_track("vid_x"))
    assert track.item_id == "vid_x"
    assert track.name == "vid_x"


def test_get_track_normalizes_video_details(provider):
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={
            "videoDetails": {
                "videoId": "vid_y",
                "title": "Some Song",
                "lengthSeconds": "200",
                "author": "Author",
                "thumbnail": {"thumbnails": []},
            }
        }
    )
    provider._ytmusic = mock
    track = asyncio.run(provider.get_track("vid_y"))
    assert track.item_id == "vid_y"
    assert track.name == "Some Song"
    assert track.duration == 200


def test_get_artist_unknown_prefix_returns_stub(provider):
    artist = asyncio.run(provider.get_artist("unknown_Foo Bar"))
    assert artist.name == "Foo Bar"
    assert artist.item_id == "unknown_Foo Bar"


def test_library_methods_no_op_when_not_authenticated(provider):
    """Library generators should yield nothing when auth is off."""
    provider._authenticated = False

    async def _consume(generator):
        return [item async for item in generator]

    assert asyncio.run(_consume(provider.get_library_artists())) == []
    assert asyncio.run(_consume(provider.get_library_albums())) == []
    assert asyncio.run(_consume(provider.get_library_tracks())) == []
    assert asyncio.run(_consume(provider.get_library_playlists())) == []


def test_library_add_remove_short_circuit_when_not_authenticated(provider):
    provider._authenticated = False
    item = MagicMock()
    item.media_type = MediaType.ARTIST
    item.provider_mappings = []
    assert asyncio.run(provider.library_add(item)) is False
    assert asyncio.run(provider.library_remove("UC1", MediaType.ARTIST)) is False


def test_recommendations_empty_when_not_authenticated(provider):
    provider._authenticated = False
    result = asyncio.run(provider.recommendations())
    assert result == []
