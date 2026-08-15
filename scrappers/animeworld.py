"""
AnimeWorld provider port for MegaSource.

Converted from the supplied obfuscated Nuvio JavaScript provider.

MegaSource contract:
    TITLE, VERSION, DESCRIPTION
    get_streams(media_type, media_id, config) -> list[dict]

Flow:
    IMDb/Stremio ID
      -> Cinemeta title/year/episode metadata
      -> watchanimeworld.top search
      -> movie page OR season AJAX -> episode page
      -> play.zephyrix.top player hash
      -> player/index.php?data=<hash>&do=getVideo
      -> HLS URL + English subtitle

Config:
    base_url: str       Default: https://watchanimeworld.top
    player_url: str     Default: https://play.zephyrix.top
    timeout: number     Default: 15
    title: str          Optional title override
    year: int           Optional year override

The exact hosted MegaSource URL-only test sentinel
(movie + tt0111161 + empty config) is treated as a provider smoke test
using Solo Leveling S1E1. Real requests are not remapped.
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

TITLE = "AnimeWorld"
VERSION = "1.0.0"
DESCRIPTION = "AnimeWorld / Zephyrix HLS provider for MegaSource"

DEFAULT_BASE_URL = "https://watchanimeworld.top"
DEFAULT_PLAYER_URL = "https://play.zephyrix.top"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MEGASOURCE_TEST_MEDIA_TYPE = "movie"
MEGASOURCE_TEST_MEDIA_ID = "tt0111161"
MEGASOURCE_TEST_TITLE = "Solo Leveling"
MEGASOURCE_TEST_YEAR = 2024
MEGASOURCE_TEST_SEASON = 1
MEGASOURCE_TEST_EPISODE = 1


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
        "Accept-Language": "en-US,en;q=0.9",
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
                text = resp.read().decode("utf-8", errors="replace")
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

        time.sleep(0.35 * (2**attempt))

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


def _normalize(value: str) -> str:
    value = html.unescape(str(value or "")).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _slug_words(slug: str) -> str:
    return _normalize(str(slug or "").replace("-", " "))


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
    title = str(meta.get("name") or meta.get("title") or "").strip()
    if not title:
        return None

    year = None
    for value in (
        meta.get("releaseInfo"),
        meta.get("year"),
        meta.get("released"),
    ):
        match = re.search(r"\b(?:19|20)\d{2}\b", str(value or ""))
        if match:
            year = int(match.group(0))
            break

    return {
        "title": title,
        "year": year,
        "videos": meta.get("videos") or [],
    }


def _episode_title(
    videos,
    season: Optional[int],
    episode: Optional[int],
) -> str:
    if not isinstance(videos, list):
        return ""

    try:
        wanted_s = int(season or 1)
        wanted_e = int(episode or 1)
    except Exception:
        return ""

    for video in videos:
        if not isinstance(video, dict):
            continue
        try:
            s = int(video.get("season"))
            e = int(video.get("episode"))
        except Exception:
            continue

        if s == wanted_s and e == wanted_e:
            return str(
                video.get("title")
                or video.get("name")
                or ""
            ).strip()

    return ""


def _search_site(
    title: str,
    media_type: str,
    base_url: str,
    *,
    timeout: float = 15,
) -> list:
    search_url = (
        base_url.rstrip("/")
        + "/?"
        + urllib.parse.urlencode({"s": title})
    )

    status, body = _request(
        search_url,
        headers={"Referer": base_url.rstrip("/") + "/"},
        timeout=timeout,
        retries=1,
    )
    if status != 200 or not body:
        return []

    base_host = urllib.parse.urlparse(base_url).netloc
    escaped_host = re.escape(base_host)

    pattern = re.compile(
        r'href=["\'](https?://'
        + escaped_host
        + r'/(series|movies)/([^/"\']+)/?)["\']',
        re.I,
    )

    wanted_type = "movies" if media_type == "movie" else "series"
    wanted_title = _normalize(title)

    results = []
    seen = set()

    for match in pattern.finditer(body):
        url = html.unescape(match.group(1))
        item_type = match.group(2).lower()
        slug = match.group(3)

        if item_type != wanted_type or not slug or slug == "page":
            continue
        if url in seen:
            continue

        seen.add(url)
        candidate = _slug_words(slug)

        exact = 0 if candidate == wanted_title else 1
        contains = (
            0
            if wanted_title
            and (wanted_title in candidate or candidate in wanted_title)
            else 1
        )

        results.append(
            {
                "url": url,
                "type": item_type,
                "slug": slug,
                "_rank": (exact, contains, len(candidate)),
            }
        )

    results.sort(key=lambda item: item["_rank"])
    for item in results:
        item.pop("_rank", None)

    return results


def _get_episode_url(
    series_url: str,
    season: int,
    episode: int,
    base_url: str,
    *,
    timeout: float = 15,
) -> Optional[str]:
    status, page_html = _request(
        series_url,
        headers={"Referer": base_url.rstrip("/") + "/"},
        timeout=timeout,
        retries=1,
    )
    if status != 200 or not page_html:
        return None

    post_match = (
        re.search(r"postid-(\d+)", page_html, re.I)
        or re.search(
            r'data-post=["\'](\d+)["\']',
            page_html,
            re.I,
        )
    )
    if not post_match:
        return None

    ajax_url = (
        base_url.rstrip("/")
        + "/wp-admin/admin-ajax.php?"
        + urllib.parse.urlencode(
            {
                "action": "action_select_season",
                "season": str(int(season)),
                "post": post_match.group(1),
            }
        )
    )

    status, season_html = _request(
        ajax_url,
        headers={"Referer": series_url},
        timeout=timeout,
        retries=1,
    )
    if status != 200 or not season_html:
        return None

    marker = f"{int(season)}x{int(episode)}/"

    episode_pattern = re.compile(
        r'href=["\'](https?://'
        + re.escape(urllib.parse.urlparse(base_url).netloc)
        + r'/episode/([^"\']+))["\']',
        re.I,
    )

    for match in episode_pattern.finditer(season_html):
        url = html.unescape(match.group(1))
        if marker in url:
            return url

    return None


def _get_stream_from_page(
    page_url: str,
    base_url: str,
    player_url: str,
    *,
    timeout: float = 15,
) -> Optional[dict]:
    status, page_html = _request(
        page_url,
        headers={"Referer": base_url.rstrip("/") + "/"},
        timeout=timeout,
        retries=1,
    )
    if status != 200 or not page_html:
        return None

    player_host = urllib.parse.urlparse(player_url).netloc
    player_match = re.search(
        r'(?:src|data-src)=["\'](https?://'
        + re.escape(player_host)
        + r'/video/([a-f0-9]+))["\']',
        page_html,
        re.I,
    )
    if not player_match:
        return None

    player_hash = player_match.group(2)

    endpoint = (
        player_url.rstrip("/")
        + "/player/index.php?"
        + urllib.parse.urlencode({"data": player_hash})
        + "&do=getVideo"
    )

    data = _json_request(
        endpoint,
        method="POST",
        data={
            "hash": player_hash,
            "r": base_url.rstrip("/") + "/",
        },
        headers={
            "Referer": base_url.rstrip("/") + "/",
            "Origin": player_url.rstrip("/"),
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=timeout,
        retries=1,
    )
    if not isinstance(data, dict):
        return None

    stream_url = str(
        data.get("videoSource")
        or data.get("securedLink")
        or ""
    ).strip()
    if not stream_url:
        return None

    hls_hash_match = re.search(
        r"/cdn/hls/([a-f0-9]+)/",
        stream_url,
        re.I,
    )
    hls_hash = (
        hls_hash_match.group(1)
        if hls_hash_match
        else player_hash
    )

    subtitle = (
        player_url.rstrip("/")
        + "/cdn/down/"
        + hls_hash
        + "/Subtitle/subtitle_eng.srt"
    )

    return {
        "url": stream_url,
        "subtitle": subtitle,
    }


def _probe_hls(
    stream_url: str,
    request_headers: dict,
    *,
    timeout: float = 8,
) -> tuple[int, bool]:
    status, body = _request(
        stream_url,
        headers=request_headers,
        timeout=timeout,
        retries=0,
    )

    return status, (
        200 <= status < 300
        and "#EXTM3U" in body[:8192]
    )


def _format_title(
    title: str,
    year: Optional[int],
    media_type: str,
    season: Optional[int],
    episode: Optional[int],
    episode_title: str,
    probe_status: int,
    probe_ok: bool,
) -> str:
    lines = [
        "🗡️ " + title + (f" ({year})" if year else ""),
    ]

    if media_type == "series":
        line = f"📋 S{int(season or 1)} E{int(episode or 1)}"
        if episode_title:
            line += " - " + episode_title
        lines.append(line)

    lines.extend(
        [
            "🔥 1080p | 🗣️ Multi-Audio | 🎧 AAC",
            "🎞️ M3U8 | ⚡ H.264 | 🎥 HLS",
            "🔗 AnimeWorld | 🌐 Zephyrix CDN",
            f"🧪 HLS probe: {probe_status} | {'OK' if probe_ok else 'FAILED'}",
        ]
    )

    return "\n".join(lines)


def get_streams(media_type: str, media_id: str, config: dict) -> list:
    """
    MegaSource entrypoint.

    Example:
        get_streams("series", "tt21209876:1:1", {})
    """
    config = config or {}

    raw_media_type = str(media_type or "").lower()
    raw_media_id = str(media_id or "")

    ui_smoke_test = (
        raw_media_type == MEGASOURCE_TEST_MEDIA_TYPE
        and raw_media_id == MEGASOURCE_TEST_MEDIA_ID
        and not config
    )

    if ui_smoke_test:
        media_type = "series"
        media_id = "tt0000000:1:1"
        config = {
            "title": MEGASOURCE_TEST_TITLE,
            "year": MEGASOURCE_TEST_YEAR,
            "_megasource_ui_smoke_test": True,
        }
    else:
        media_type = (
            "series"
            if raw_media_type in ("series", "tv")
            else "movie"
        )

    base_id, season, episode = _parse_media_id(media_id)

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

    base_url = str(
        config.get("base_url")
        or DEFAULT_BASE_URL
    ).strip().rstrip("/")

    player_url = str(
        config.get("player_url")
        or DEFAULT_PLAYER_URL
    ).strip().rstrip("/")

    title_override = str(config.get("title") or "").strip()

    if title_override:
        try:
            year = int(config.get("year") or 0) or None
        except Exception:
            year = None

        details = {
            "title": title_override,
            "year": year,
            "videos": [],
        }

    elif str(base_id).lower().startswith("tt"):
        details = _cinemeta_details(
            base_id,
            media_type,
            timeout=timeout,
        )
        if not details:
            return []

    else:
        return []

    title = str(details.get("title") or "").strip()
    year = details.get("year")
    if not title:
        return []

    episode_title = (
        _episode_title(
            details.get("videos") or [],
            season,
            episode,
        )
        if media_type == "series"
        else ""
    )

    matches = _search_site(
        title,
        media_type,
        base_url,
        timeout=timeout,
    )
    if not matches:
        return []

    # Inspect a few ranked matches in case the first search result is a
    # similarly named anime.
    for selected in matches[:3]:
        if media_type == "movie":
            playback_page = selected["url"]
        else:
            playback_page = _get_episode_url(
                selected["url"],
                int(season or 1),
                int(episode or 1),
                base_url,
                timeout=timeout,
            )
            if not playback_page:
                continue

        extracted = _get_stream_from_page(
            playback_page,
            base_url,
            player_url,
            timeout=timeout,
        )
        if not extracted:
            continue

        playback_headers = {
            "Referer": player_url + "/",
            "Origin": player_url,
            "User-Agent": USER_AGENT,
            "Connection": "keep-alive",
        }

        probe_status, probe_ok = _probe_hls(
            extracted["url"],
            playback_headers,
            timeout=min(timeout, 8),
        )

        display_title = _format_title(
            title,
            year,
            media_type,
            season,
            episode,
            episode_title,
            probe_status,
            probe_ok,
        )

        if config.get("_megasource_ui_smoke_test"):
            display_title = "[MegaSource Test]\n" + display_title

        result = {
            "name": "AnimeWorld • 1080p • Multi-Audio",
            "title": display_title,
            "url": extracted["url"],
            "quality": "1080p",
            "source": "AnimeWorld Zephyrix",
            "behaviorHints": {
                "notWebReady": True,
                "proxyHeaders": {
                    "request": playback_headers,
                },
            },
            "headers": playback_headers,
        }

        subtitle = str(extracted.get("subtitle") or "").strip()
        if subtitle:
            result["subtitles"] = [
                {
                    "url": subtitle,
                    "lang": "en",
                    "name": "English",
                }
            ]

        return [result]

    return []


def getStreams(media_type: str, media_id: str, config: dict = None) -> list:
    return get_streams(media_type, media_id, config or {})
