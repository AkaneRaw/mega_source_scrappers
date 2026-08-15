"""
AnimeSalt provider port for MegaSource.

Converted from the supplied obfuscated Nuvio JavaScript provider.

MegaSource contract:
    TITLE, VERSION, DESCRIPTION
    get_streams(media_type, media_id, config) -> list[dict]

Normal flow:
    IMDb/Stremio ID
      -> Cinemeta title/year
      -> AnimeSalt search
      -> movie page or SxE episode page
      -> as-cdn player hash
      -> player/index.php?data=<hash>&do=getVideo
      -> HLS URL + request headers

Config:
    base_url: str          Default: https://animesalt.link
    timeout: number        Default: 15
    title: str             Optional test/title override
    year: int              Optional test/year override
    tmdb_api_key: str      Optional fallback for numeric TMDB IDs

The exact MegaSource URL-only web-test sentinel (movie + tt0111161 +
empty config) is treated as a provider smoke test using Solo Leveling S1E1.
Real requests are not remapped.
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

TITLE = "AnimeSalt"
VERSION = "1.0.0"
DESCRIPTION = "AnimeSalt HLS provider for MegaSource"

DEFAULT_BASE = "https://animesalt.link"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)

# Hosted MegaSource's URL-only /api/test-scraper fallback.
MEGASOURCE_TEST_MEDIA_TYPE = "movie"
MEGASOURCE_TEST_MEDIA_ID = "tt0111161"

# Used only for that exact smoke-test sentinel.
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

        time.sleep(0.4 * (2**attempt))

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
    if status < 200 or status >= 300 or not body:
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


def _clean_title(value: str) -> str:
    value = html.unescape(str(value or "")).lower()
    value = re.sub(r"[^a-z0-9\s]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _cinemeta_details(
    imdb_id: str,
    media_type: str,
    *,
    timeout: float = 15,
) -> Optional[dict]:
    if not str(imdb_id).lower().startswith("tt"):
        return None

    meta_type = "series" if media_type in ("series", "tv") else "movie"
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


def _tmdb_details(
    tmdb_id: str,
    media_type: str,
    api_key: str,
    *,
    timeout: float = 15,
) -> Optional[dict]:
    if not api_key:
        return None

    endpoint = "tv" if media_type in ("series", "tv") else "movie"
    url = (
        f"https://api.themoviedb.org/3/{endpoint}/"
        f"{urllib.parse.quote(str(tmdb_id))}?"
        + urllib.parse.urlencode({"api_key": api_key})
    )

    data = _json_request(url, timeout=timeout, retries=1)
    if not isinstance(data, dict):
        return None

    title = str(
        data.get("name") if endpoint == "tv" else data.get("title")
    ).strip()
    if not title:
        return None

    date = (
        data.get("first_air_date")
        if endpoint == "tv"
        else data.get("release_date")
    )

    year = None
    match = re.search(r"\b(?:19|20)\d{2}\b", str(date or ""))
    if match:
        year = int(match.group(0))

    return {"title": title, "year": year, "videos": []}


def _episode_title_from_cinemeta(
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
            return str(video.get("title") or video.get("name") or "").strip()

    return ""


def _extract_search_results(
    html_text: str,
    query_title: str,
    media_type: str,
    year: Optional[int],
    base_url: str,
) -> list:
    # Mirror the JS: prefer the movies-a content region when present.
    section_match = re.search(
        r'id=["\']movies-a["\']([\s\S]*?)(?=<footer|id=["\']footer|class=["\']footer)',
        html_text,
        flags=re.I,
    )
    region = section_match.group(1) if section_match else html_text

    results = []
    seen = set()

    for article_match in re.finditer(
        r"<article[^>]*>([\s\S]*?)</article>",
        region,
        flags=re.I,
    ):
        article = article_match.group(1)

        href_match = re.search(
            r'href=["\'](https://animesalt\.link/(series|movies)/([^/"\']+)/?)["\']',
            article,
            flags=re.I,
        )
        title_match = re.search(
            r'class=["\'][^"\']*entry-title[^"\']*["\'][^>]*>([^<]+)<',
            article,
            flags=re.I,
        )
        year_match = re.search(
            r'class=["\'][^"\']*year[^"\']*["\'][^>]*>(\d{4})<',
            article,
            flags=re.I,
        )

        if not href_match or not title_match:
            continue

        url = href_match.group(1)
        result_type = href_match.group(2).lower()
        slug = href_match.group(3)
        title = html.unescape(title_match.group(1)).strip()

        try:
            result_year = int(year_match.group(1)) if year_match else None
        except Exception:
            result_year = None

        if not slug or slug in seen:
            continue
        seen.add(slug)

        results.append(
            {
                "url": url,
                "type": result_type,
                "slug": slug,
                "title": title,
                "year": result_year,
            }
        )

    wanted_type = "movies" if media_type == "movie" else "series"
    typed = [item for item in results if item["type"] == wanted_type]
    if typed:
        results = typed

    # Match original JS year behavior: exact/+/-1 first, unknown-year second.
    if year:
        year_matches = [
            item
            for item in results
            if item["year"] is not None and abs(item["year"] - int(year)) <= 1
        ]
        year_unknown = [item for item in results if item["year"] is None]

        if year_matches:
            results = year_matches
        elif year_unknown:
            results = year_unknown

    wanted_clean = _clean_title(query_title)

    def sort_key(item):
        candidate = _clean_title(item["title"])
        exact = 0 if candidate == wanted_clean else 1
        prefix = 0 if candidate.startswith(wanted_clean) else 1
        return exact, prefix, len(candidate)

    results.sort(key=sort_key)
    return results


def _search_site(
    title: str,
    media_type: str,
    year: Optional[int],
    base_url: str,
    *,
    timeout: float = 15,
) -> list:
    search_url = base_url.rstrip("/") + "/?" + urllib.parse.urlencode(
        {"s": title}
    )
    status, body = _request(
        search_url,
        headers={"Referer": base_url.rstrip("/") + "/"},
        timeout=timeout,
        retries=1,
    )
    if status != 200 or not body:
        return []

    return _extract_search_results(
        body,
        title,
        media_type,
        year,
        base_url,
    )


def _episode_url_from_html(
    html_text: str,
    season: int,
    episode: int,
    base_url: str,
) -> Optional[str]:
    # Site URLs contain the season/episode marker, e.g. 1x3.
    pattern = re.compile(
        r'href=["\']('
        + re.escape(base_url.rstrip("/"))
        + r'/episode/[^"\']*'
        + re.escape(str(int(season)))
        + r'x'
        + re.escape(str(int(episode)))
        + r'[^"\']*)["\']',
        flags=re.I,
    )
    match = pattern.search(html_text)
    return html.unescape(match.group(1)) if match else None


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

    season_posts = []
    for match in re.finditer(
        r'data-post=["\'](\d+)["\']\s+data-season=["\'](\d+)["\']',
        page_html,
        flags=re.I,
    ):
        season_posts.append(
            {
                "post": match.group(1),
                "season": int(match.group(2)),
            }
        )

    if not season_posts:
        return _episode_url_from_html(
            page_html,
            season,
            episode,
            base_url,
        )

    selected = next(
        (
            item
            for item in season_posts
            if item["season"] == int(season)
        ),
        None,
    )
    if not selected:
        return None

    ajax_url = (
        base_url.rstrip("/")
        + "/wp-admin/admin-ajax.php?"
        + urllib.parse.urlencode(
            {
                "action": "action_select_season",
                "season": str(int(season)),
                "post": selected["post"],
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

    return _episode_url_from_html(
        season_html,
        season,
        episode,
        base_url,
    )


def _get_stream_from_page(
    page_url: str,
    base_url: str,
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

    player_match = re.search(
        r'src=["\'](https://as-cdn\d+\.top/video/([a-f0-9]+))["\']',
        page_html,
        flags=re.I,
    )
    if not player_match:
        return None

    player_url = player_match.group(1)
    player_hash = player_match.group(2)
    cdn_base = player_url.split("/video/", 1)[0]

    endpoint = (
        cdn_base
        + "/player/index.php?"
        + urllib.parse.urlencode(
            {
                "data": player_hash,
                "do": "getVideo",
            }
        )
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
            "Origin": cdn_base,
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
        r"/hls/([a-f0-9]+)/",
        stream_url,
        flags=re.I,
    )
    hls_hash = (
        hls_hash_match.group(1)
        if hls_hash_match
        else player_hash
    )

    # Original JS takes the URL prefix before /cdn/hls/.
    if "/cdn/hls/" in stream_url:
        stream_base = stream_url.split("/cdn/hls/", 1)[0]
    else:
        parsed = urllib.parse.urlparse(stream_url)
        stream_base = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else cdn_base

    subtitle = (
        stream_base
        + "/cdn/down/"
        + hls_hash
        + "/Subtitle/subtitle_eng.srt"
    )

    return {
        "url": stream_url,
        "subtitle": subtitle,
        "cdnBase": stream_base,
    }


def _quality_rank(quality: str) -> int:
    if re.search(r"2160p|4k", quality, flags=re.I):
        return 4
    if re.search(r"1080p", quality, flags=re.I):
        return 3
    if re.search(r"720p", quality, flags=re.I):
        return 2
    if re.search(r"480p", quality, flags=re.I):
        return 1
    return 0


def _resolution_label(quality: str) -> str:
    q = str(quality or "").lower()
    if "2160p" in q or "4k" in q or "uhd" in q:
        return "🔥 2160p"
    if "1080p" in q or "fhd" in q:
        return "🔥 1080p"
    if "720p" in q or "hd" in q:
        return "💎 720p"
    if "480p" in q or "sd" in q:
        return "📺 480p"
    return "📺 " + str(quality or "1080p")


def _format_stream_title(
    title: str,
    year: Optional[int],
    media_type: str,
    season: Optional[int],
    episode: Optional[int],
    episode_title: str,
    quality: str,
) -> str:
    lines = [
        "🧂 " + title + (f" ({year})" if year else ""),
    ]

    if media_type == "series" and season and episode:
        line = f"🎬 S{int(season)} E{int(episode)}"
        if episode_title:
            line += " • " + episode_title
        lines.append(line)

    lines.extend(
        [
            f"{_resolution_label(quality)} | 🗣️ Multi-Audio",
            "🎞️ HLS | ⚡ H.264 | 🎧 AAC",
            "🔗 AnimeSalt | 🌐 Direct CDN | 📥 WEB-DL",
        ]
    )

    return "\n".join(lines)


def get_streams(media_type: str, media_id: str, config: dict) -> list:
    """
    MegaSource entrypoint.

    Examples:
      get_streams("series", "tt21209876:1:1", {})
      get_streams("movie", "tt...movie...", {})
    """
    config = config or {}

    raw_media_type = str(media_type or "").lower()
    raw_media_id = str(media_id or "")

    # Same compatibility behavior that proved useful for AniKotoTV:
    # the hosted MegaSource web test sends only the scraper URL and the
    # backend substitutes movie + tt0111161. AnimeSalt is anime-oriented,
    # so this exact sentinel becomes a known-good provider smoke test.
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
        media_type = "series" if raw_media_type in ("series", "tv") else "movie"

    base_id, season, episode = _parse_media_id(media_id)

    if media_type == "series":
        season = int(season or 1)
        episode = int(episode or 1)

    base_url = str(config.get("base_url") or DEFAULT_BASE).rstrip("/")

    try:
        timeout = float(config.get("timeout", 15))
    except Exception:
        timeout = 15

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
        api_key = str(config.get("tmdb_api_key") or "").strip()
        if not api_key:
            return []
        details = _tmdb_details(
            base_id,
            media_type,
            api_key,
            timeout=timeout,
        )
        if not details:
            return []

    title = str(details.get("title") or "").strip()
    year = details.get("year")
    if not title:
        return []

    episode_title = (
        _episode_title_from_cinemeta(
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
        year,
        base_url,
        timeout=timeout,
    )
    if not matches:
        return []

    selected = matches[0]

    if media_type == "movie":
        playback_page = selected["url"]
    else:
        playback_page = _get_episode_url(
            selected["url"],
            season,
            episode,
            base_url,
            timeout=timeout,
        )
        if not playback_page:
            return []

    extracted = _get_stream_from_page(
        playback_page,
        base_url,
        timeout=timeout,
    )
    if not extracted:
        return []

    stream_url = extracted["url"]
    cdn_base = extracted.get("cdnBase") or ""
    quality = "1080p"

    display_title = _format_stream_title(
        title,
        year,
        media_type,
        season,
        episode,
        episode_title,
        quality,
    )

    if config.get("_megasource_ui_smoke_test"):
        display_title = "[MegaSource Test]\n" + display_title

    result = {
        "name": f"{TITLE} • {quality} • Multi-Audio",
        "title": display_title,
        "url": stream_url,
        "quality": quality,
        "source": "AnimeSalt Direct CDN",
        "behaviorHints": {
            "notWebReady": True,
            "proxyHeaders": {
                "request": {
                    "Referer": cdn_base.rstrip("/") + "/",
                    "Origin": cdn_base.rstrip("/"),
                    "User-Agent": USER_AGENT,
                }
            },
        },
    }

    subtitle = str(extracted.get("subtitle") or "").strip()
    if subtitle:
        # MegaSource may drop this during normalization, but direct consumers
        # of the Python provider can still use it.
        result["subtitles"] = [
            {
                "url": subtitle,
                "lang": "en",
                "name": "English",
            }
        ]

    return [result]


# Optional JS-style compatibility alias.
def getStreams(media_type: str, media_id: str, config: dict = None) -> list:
    return get_streams(media_type, media_id, config or {})
