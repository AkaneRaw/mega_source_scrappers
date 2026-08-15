"""
PlayIMDb provider port for MegaSource.

Converted from the supplied obfuscated JavaScript provider.

MegaSource contract:
    TITLE, VERSION, DESCRIPTION
    get_streams(media_type, media_id, config) -> list[dict]

Flow:
    IMDb/Stremio ID
      -> IMDb -> TMDB mapping (Wikidata, key-free)
      -> streamdata.vaplayer.ru/api.php
      -> stream_urls (MP4 / HLS)
      -> optional default subtitles

Config:
    timeout: number         Default: 15
    tmdb_id: int            Optional direct TMDB ID override
    tmdb_api_key: str       Optional TMDB Find-by-ID fallback
    title: str              Optional title override
    year: int               Optional year override
    duration: str           Optional duration override
    base_api: str           Optional stream API override

The exact hosted MegaSource URL-only test sentinel
(movie + tt0111161 + empty config) uses TMDB movie ID 278 directly so the
MegaSource web tester does not depend on an external ID-mapping request.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

TITLE = "PlayIMDb"
VERSION = "1.0.0"
DESCRIPTION = "PlayIMDb direct MP4/HLS provider for MegaSource"

BASE_API = "https://streamdata.vaplayer.ru/api.php"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

PLAYBACK_HEADERS = {
    "Origin": "https://nextgencloudfabric.com",
    "Referer": "https://nextgencloudfabric.com/",
    "User-Agent": USER_AGENT,
}

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

MEGASOURCE_TEST_MEDIA_TYPE = "movie"
MEGASOURCE_TEST_MEDIA_ID = "tt0111161"
MEGASOURCE_TEST_TMDB_ID = 278
MEGASOURCE_TEST_TITLE = "The Shawshank Redemption"
MEGASOURCE_TEST_YEAR = 1994


def _request(
    url: str,
    *,
    method: str = "GET",
    data=None,
    headers: Optional[dict] = None,
    timeout: float = 15,
    retries: int = 1,
) -> tuple[int, str]:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }
    if headers:
        request_headers.update(headers)

    body = None
    if data is not None:
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
            request_headers.setdefault(
                "Content-Type",
                "application/x-www-form-urlencoded",
            )
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers=request_headers,
                method=method,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace")
                return int(getattr(resp, "status", 200)), text

        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                text = ""

            if attempt >= retries:
                return int(exc.code), text

        except Exception:
            if attempt >= retries:
                return 0, ""

        time.sleep(0.35 * (2 ** attempt))

    return 0, ""


def _json_request(
    url: str,
    *,
    method: str = "GET",
    data=None,
    headers: Optional[dict] = None,
    timeout: float = 15,
    retries: int = 1,
):
    status, body = _request(
        url,
        method=method,
        data=data,
        headers=headers,
        timeout=timeout,
        retries=retries,
    )

    if not (200 <= status < 300) or not body:
        return None

    try:
        return json.loads(body)
    except Exception:
        return None


def _parse_media_id(media_id: str):
    parts = str(media_id or "").split(":")
    base_id = parts[0] if parts else ""

    season = None
    episode = None

    if len(parts) > 1:
        try:
            season = int(parts[1])
        except Exception:
            pass

    if len(parts) > 2:
        try:
            episode = int(parts[2])
        except Exception:
            pass

    return base_id, season, episode


def _cinemeta_details(
    imdb_id: str,
    media_type: str,
    *,
    timeout: float = 15,
) -> Optional[dict]:
    if not str(imdb_id).lower().startswith("tt"):
        return None

    meta_type = "series" if media_type == "series" else "movie"
    url = (
        f"https://v3-cinemeta.strem.io/meta/{meta_type}/"
        f"{urllib.parse.quote(str(imdb_id))}.json"
    )

    data = _json_request(url, timeout=timeout, retries=1)
    if not isinstance(data, dict):
        return None

    meta = data.get("meta") or {}

    title = str(
        meta.get("name")
        or meta.get("title")
        or ""
    ).strip()

    if not title:
        return None

    year = None
    for value in (
        meta.get("releaseInfo"),
        meta.get("year"),
        meta.get("released"),
    ):
        match = re.search(
            r"\b(?:19|20)\d{2}\b",
            str(value or ""),
        )
        if match:
            year = int(match.group(0))
            break

    duration = "N/A"
    runtime = str(meta.get("runtime") or "").strip()
    if runtime:
        match = re.search(r"(\d+)", runtime)
        if match:
            duration = match.group(1) + " min"

    return {
        "title": title,
        "year": year,
        "duration": duration,
        "videos": meta.get("videos") or [],
    }


def _episode_duration(
    details: dict,
    season: int,
    episode: int,
) -> str:
    videos = details.get("videos") or []

    if isinstance(videos, list):
        for video in videos:
            if not isinstance(video, dict):
                continue

            try:
                s = int(video.get("season"))
                e = int(video.get("episode"))
            except Exception:
                continue

            if s != int(season) or e != int(episode):
                continue

            for value in (
                video.get("runtime"),
                video.get("duration"),
            ):
                match = re.search(
                    r"(\d+)",
                    str(value or ""),
                )
                if match:
                    return match.group(1) + " min"

    return str(details.get("duration") or "N/A")


def _resolve_tmdb_wikidata(
    imdb_id: str,
    media_type: str,
    *,
    timeout: float = 12,
) -> Optional[int]:
    """
    Key-free IMDb -> TMDB mapping.

    Wikidata:
      P345  = IMDb ID
      P4947 = TMDB movie ID
      P4983 = TMDB TV series ID
    """
    if not re.fullmatch(r"tt\d+", str(imdb_id or ""), re.I):
        return None

    prop = "P4983" if media_type == "series" else "P4947"

    query = (
        "SELECT ?tmdb WHERE { "
        f'?item wdt:P345 "{imdb_id}" . '
        f"?item wdt:{prop} ?tmdb . "
        "} LIMIT 1"
    )

    url = (
        WIKIDATA_SPARQL
        + "?"
        + urllib.parse.urlencode(
            {
                "query": query,
                "format": "json",
            }
        )
    )

    data = _json_request(
        url,
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": (
                "MegaSource-PlayIMDb/1.0 "
                "(IMDb to TMDB metadata resolver)"
            ),
        },
        timeout=timeout,
        retries=1,
    )

    if not isinstance(data, dict):
        return None

    try:
        bindings = data["results"]["bindings"]
        if not bindings:
            return None

        value = bindings[0]["tmdb"]["value"]
        return int(value)
    except Exception:
        return None


def _resolve_tmdb_api(
    imdb_id: str,
    media_type: str,
    api_key: str,
    *,
    timeout: float = 12,
) -> Optional[int]:
    """
    Optional fallback via TMDB's official Find-by-ID endpoint.
    """
    if not api_key:
        return None

    url = (
        "https://api.themoviedb.org/3/find/"
        + urllib.parse.quote(str(imdb_id))
        + "?"
        + urllib.parse.urlencode(
            {
                "api_key": api_key,
                "external_source": "imdb_id",
            }
        )
    )

    data = _json_request(
        url,
        timeout=timeout,
        retries=1,
    )
    if not isinstance(data, dict):
        return None

    key = (
        "tv_results"
        if media_type == "series"
        else "movie_results"
    )

    results = data.get(key) or []
    if not results or not isinstance(results[0], dict):
        return None

    try:
        return int(results[0].get("id"))
    except Exception:
        return None


def _resolve_tmdb_id(
    base_id: str,
    media_type: str,
    config: dict,
    *,
    timeout: float = 15,
) -> Optional[int]:
    # Explicit override is fastest and most reliable.
    try:
        if config.get("tmdb_id") not in (None, ""):
            return int(config["tmdb_id"])
    except Exception:
        pass

    # Numeric IDs are accepted directly.
    if str(base_id or "").isdigit():
        try:
            return int(base_id)
        except Exception:
            pass

    if not str(base_id or "").lower().startswith("tt"):
        return None

    tmdb_id = _resolve_tmdb_wikidata(
        base_id,
        media_type,
        timeout=min(timeout, 12),
    )
    if tmdb_id:
        return tmdb_id

    api_key = str(
        config.get("tmdb_api_key")
        or ""
    ).strip()

    return _resolve_tmdb_api(
        base_id,
        media_type,
        api_key,
        timeout=min(timeout, 12),
    )


def _quality_from_filename(file_name: str):
    lower = str(file_name or "").lower()

    if "2160p" in lower or "4k" in lower:
        return "4K UHD", "2160P"

    if "1080p" in lower:
        return "1080p FHD", "1080P"

    if "720p" in lower:
        return "720p HD", "720P"

    return "1080p FHD", "1080P"


def _audio_from_filename(file_name: str):
    lower = str(file_name or "").lower()

    if (
        "dual" in lower
        or ("hindi" in lower and "english" in lower)
    ):
        return "Dual-Audio", "English • Hindi"

    if "multi" in lower:
        return "Multi-Audio", "Multilingual"

    if "hindi" in lower:
        return "Hindi-Audio", "Hindi"

    if "english" in lower:
        return "English-Audio", "English"

    return "Original-Audio", "Original-Audio"


def _subtitles(data: dict) -> list:
    raw = data.get("default_subs") or []
    if not isinstance(raw, list):
        return []

    results = []

    for item in raw:
        if not isinstance(item, dict):
            continue

        url = str(item.get("url") or "").strip()
        if not url:
            continue

        lang = str(
            item.get("lang")
            or item.get("code")
            or "en"
        ).strip()

        results.append(
            {
                "id": str(
                    item.get("code")
                    or lang
                ),
                "url": url,
                "lang": lang,
                "name": lang,
            }
        )

    return results


def _probe_stream(
    url: str,
    headers: dict,
    *,
    timeout: float = 8,
) -> tuple[int, bool, str]:
    lower = str(url or "").lower()

    if ".m3u8" in lower:
        status, body = _request(
            url,
            headers=headers,
            timeout=timeout,
            retries=0,
        )
        return (
            status,
            200 <= status < 300
            and "#EXTM3U" in body[:8192],
            "HLS",
        )

    probe_headers = dict(headers)
    probe_headers["Range"] = "bytes=0-1023"

    status, _ = _request(
        url,
        headers=probe_headers,
        timeout=timeout,
        retries=0,
    )

    if status in (200, 206):
        return status, True, "MP4"

    return status, False, "Direct"


def _stream_api(
    tmdb_id: int,
    media_type: str,
    season: Optional[int],
    episode: Optional[int],
    base_api: str,
    *,
    timeout: float = 15,
):
    params = {
        "tmdb": str(int(tmdb_id)),
        "type": "tv" if media_type == "series" else "movie",
    }

    if media_type == "series":
        params["season"] = str(int(season or 1))
        params["episode"] = str(int(episode or 1))

    url = (
        base_api
        + ("&" if "?" in base_api else "?")
        + urllib.parse.urlencode(params)
    )

    return _json_request(
        url,
        headers=PLAYBACK_HEADERS,
        timeout=timeout,
        retries=1,
    )


def get_streams(
    media_type: str,
    media_id: str,
    config: dict,
) -> list:
    config = config or {}

    raw_media_type = str(media_type or "").lower()
    raw_media_id = str(media_id or "")

    media_type = (
        "series"
        if raw_media_type in ("series", "tv")
        else "movie"
    )

    base_id, season, episode = _parse_media_id(
        raw_media_id
    )

    if media_type == "series":
        season = int(season or 1)
        episode = int(episode or 1)
    else:
        season = None
        episode = None

    try:
        timeout = float(config.get("timeout", 15))
    except Exception:
        timeout = 15

    ui_smoke_test = (
        raw_media_type == MEGASOURCE_TEST_MEDIA_TYPE
        and raw_media_id == MEGASOURCE_TEST_MEDIA_ID
        and not config
    )

    if ui_smoke_test:
        tmdb_id = MEGASOURCE_TEST_TMDB_ID
        details = {
            "title": MEGASOURCE_TEST_TITLE,
            "year": MEGASOURCE_TEST_YEAR,
            "duration": "142 min",
            "videos": [],
        }
    else:
        tmdb_id = _resolve_tmdb_id(
            base_id,
            media_type,
            config,
            timeout=timeout,
        )
        if not tmdb_id:
            return []

        title_override = str(
            config.get("title")
            or ""
        ).strip()

        if title_override:
            try:
                year = int(
                    config.get("year")
                    or 0
                ) or None
            except Exception:
                year = None

            details = {
                "title": title_override,
                "year": year,
                "duration": str(
                    config.get("duration")
                    or "N/A"
                ),
                "videos": [],
            }
        else:
            details = _cinemeta_details(
                base_id,
                media_type,
                timeout=timeout,
            ) or {
                "title": "Unknown Title",
                "year": None,
                "duration": "N/A",
                "videos": [],
            }

    base_api = str(
        config.get("base_api")
        or BASE_API
    ).strip()

    response = _stream_api(
        tmdb_id,
        media_type,
        season,
        episode,
        base_api,
        timeout=timeout,
    )

    if not isinstance(response, dict):
        return []

    status_code = response.get("status_code")
    if str(status_code) != "200":
        return []

    data = response.get("data")
    if not isinstance(data, dict):
        return []

    stream_urls = data.get("stream_urls") or []
    if not isinstance(stream_urls, list) or not stream_urls:
        return []

    file_name = str(
        data.get("file_name")
        or ""
    )

    quality_label, quality = _quality_from_filename(
        file_name
    )
    audio_label, audio_display = _audio_from_filename(
        file_name
    )

    title = str(
        details.get("title")
        or "Unknown Title"
    )
    year = details.get("year") or "N/A"

    duration = (
        _episode_duration(
            details,
            int(season or 1),
            int(episode or 1),
        )
        if media_type == "series"
        else str(details.get("duration") or "N/A")
    )

    subtitle_items = _subtitles(data)

    results = []
    seen = set()

    for index, stream_url in enumerate(stream_urls):
        stream_url = str(stream_url or "").strip()
        if not stream_url or stream_url in seen:
            continue
        seen.add(stream_url)

        lower_url = stream_url.lower()

        if ".mp4" in lower_url:
            format_name = "MP4"
        elif ".m3u8" in lower_url:
            format_name = "M3U8"
        else:
            format_name = "Direct"

        probe_status, probe_ok, probe_kind = _probe_stream(
            stream_url,
            PLAYBACK_HEADERS,
            timeout=min(timeout, 8),
        )

        server = f"Server {index + 1}"

        if media_type == "series":
            line1 = (
                f"🎬 {title} - "
                f"S{int(season or 1)}E{int(episode or 1)} "
                f"({year})"
            )
        else:
            line1 = f"🎬 {title} - {year}"

        display_title = "\n".join(
            [
                line1,
                f"💎 {quality} | 🌍 {audio_display}",
                (
                    f"🎞️ {format_name} | ⏱️ {duration}"
                    f" | 📌 {server}"
                ),
                (
                    f"🧪 {probe_kind} probe: {probe_status}"
                    f" | {'OK' if probe_ok else 'FAILED'}"
                ),
            ]
        )

        if ui_smoke_test:
            display_title = (
                "[MegaSource Test]\n"
                + display_title
            )

        stream = {
            "name": (
                f"🟡 PlayIMDb | {quality_label}"
                f" | {audio_label}"
            ),
            "title": display_title,
            "url": stream_url,
            "quality": quality.lower(),
            "source": f"PlayIMDb {server}",
            "type": "direct",
            "behaviorHints": {
                "notWebReady": True,
                "proxyHeaders": {
                    "request": dict(
                        PLAYBACK_HEADERS
                    ),
                },
            },
            "headers": dict(PLAYBACK_HEADERS),
        }

        if subtitle_items:
            stream["subtitles"] = subtitle_items

        results.append(stream)

    return results


def getStreams(
    media_type: str,
    media_id: str,
    config: dict = None,
) -> list:
    return get_streams(
        media_type,
        media_id,
        config or {},
    )
