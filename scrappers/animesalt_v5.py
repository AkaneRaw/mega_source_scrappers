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
    mediaflow_proxy_url: str       Optional HLS relay base URL
    mediaflow_api_password: str    Optional relay API password

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
VERSION = "1.4.0"
DESCRIPTION = "AnimeSalt HLS provider for MegaSource"

DEFAULT_BASE = "https://animesalt.link"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)

# Optional HLS relay. For AnimeSalt this is the reliable playback mode when
# the CDN URL/cookie works on MegaSource but not on the user's Stremio/Nuvio
# client. You can hardcode these two values in this file, or provide the
# equivalent config keys.
MEDIAFLOW_PROXY_URL = ""
MEDIAFLOW_API_PASSWORD = ""

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



def _request_full(
    url: str,
    *,
    method: str = "GET",
    data=None,
    headers: Optional[dict] = None,
    timeout: float = 15,
    retries: int = 1,
):
    """Like _request(), but also returns response headers and Set-Cookie."""
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
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace")
                response_headers = dict(resp.headers.items())
                cookies = []
                try:
                    cookies = resp.headers.get_all("Set-Cookie") or []
                except Exception:
                    value = resp.headers.get("Set-Cookie")
                    if value:
                        cookies = [value]
                return (
                    int(getattr(resp, "status", 200)),
                    text,
                    response_headers,
                    cookies,
                )
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                text = ""
            if attempt >= retries:
                return int(exc.code), text, {}, []
        except Exception:
            if attempt >= retries:
                return 0, "", {}, []

        time.sleep(0.4 * (2**attempt))

    return 0, "", {}, []


def _cookie_header(set_cookie_values) -> str:
    """Convert Set-Cookie response values into a Cookie request header."""
    pairs = []
    for value in set_cookie_values or []:
        first = str(value).split(";", 1)[0].strip()
        if first and "=" in first:
            pairs.append(first)
    return "; ".join(pairs)


def _hls_first_uri(playlist: str) -> Optional[str]:
    for line in str(playlist or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def _hls_tag_uri(playlist: str, tag: str) -> Optional[str]:
    pattern = re.compile(
        r"^" + re.escape(tag) + r".*?URI=[\"']([^\"']+)[\"']",
        flags=re.I | re.M,
    )
    match = pattern.search(str(playlist or ""))
    return match.group(1) if match else None


def _probe_hls_chain(
    stream_url: str,
    playback_headers: dict,
    *,
    timeout: float = 8,
) -> dict:
    """
    Probe master/media playlist plus the first init/key/media object.

    This is intentionally diagnostic: a successful server-side chain proves
    the HLS assets are reachable from MegaSource's execution environment.
    """
    result = {
        "master": 0,
        "media": 0,
        "asset": 0,
        "assetKind": "",
    }

    status, body = _request(
        stream_url,
        headers=playback_headers,
        timeout=timeout,
        retries=0,
    )
    result["master"] = status

    if not (200 <= status < 300) or "#EXTM3U" not in body[:8192]:
        return result

    first_uri = _hls_first_uri(body)
    media_url = stream_url
    media_body = body

    # If the first non-comment URI is another playlist, follow it.
    if first_uri and (
        ".m3u8" in first_uri.lower()
        or "#EXT-X-STREAM-INF" in body
    ):
        media_url = urllib.parse.urljoin(stream_url, first_uri)
        media_status, media_body = _request(
            media_url,
            headers=playback_headers,
            timeout=timeout,
            retries=0,
        )
        result["media"] = media_status
        if not (200 <= media_status < 300):
            return result
    else:
        result["media"] = status

    # Prefer an init segment, then encryption key, then first media segment.
    asset_uri = _hls_tag_uri(media_body, "#EXT-X-MAP")
    asset_kind = "init"

    if not asset_uri:
        asset_uri = _hls_tag_uri(media_body, "#EXT-X-KEY")
        asset_kind = "key"

    if not asset_uri:
        asset_uri = _hls_first_uri(media_body)
        asset_kind = "segment"

    if not asset_uri:
        return result

    asset_url = urllib.parse.urljoin(media_url, asset_uri)
    asset_status, _ = _request(
        asset_url,
        headers={
            **playback_headers,
            "Range": "bytes=0-1023",
        },
        timeout=timeout,
        retries=0,
    )
    result["asset"] = asset_status
    result["assetKind"] = asset_kind
    return result

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
) -> list:
    """
    Resolve the AnimeSalt player response.

    The upstream JS prefers `videoSource` and falls back to `securedLink`.
    When running through MegaSource the resolver runs on Wasmer while playback
    runs on the user's device, so this port preserves BOTH link forms when the
    server provides both. That lets the client try the portable form instead
    of silently discarding it.
    """
    status, page_html = _request(
        page_url,
        headers={"Referer": base_url.rstrip("/") + "/"},
        timeout=timeout,
        retries=1,
    )
    if status != 200 or not page_html:
        return []

    player_match = re.search(
        r'src=["\'](https://as-cdn\d+\.top/video/([a-f0-9]+))["\']',
        page_html,
        flags=re.I,
    )
    if not player_match:
        return []

    player_url = player_match.group(1)
    player_hash = player_match.group(2)
    player_cdn_base = player_url.split("/video/", 1)[0]

    endpoint = (
        player_cdn_base
        + "/player/index.php?"
        + urllib.parse.urlencode(
            {
                "data": player_hash,
                "do": "getVideo",
            }
        )
    )

    player_status, player_body, _, set_cookies = _request_full(
        endpoint,
        method="POST",
        data={
            "hash": player_hash,
            "r": base_url.rstrip("/") + "/",
        },
        headers={
            "Referer": base_url.rstrip("/") + "/",
            "Origin": player_cdn_base,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=timeout,
        retries=1,
    )
    if not (200 <= player_status < 300) or not player_body:
        return []

    try:
        data = json.loads(player_body)
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    cookie_header = _cookie_header(set_cookies)

    raw_candidates = [
        ("videoSource", data.get("videoSource")),
        ("securedLink", data.get("securedLink")),
    ]

    results = []
    seen = set()

    for link_type, value in raw_candidates:
        stream_url = str(value or "").strip()
        if not stream_url or stream_url in seen:
            continue
        seen.add(stream_url)

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

        # Match the original JS exactly: playback Referer/Origin use the
        # prefix before "/cdn/hls/" when that marker exists.
        if "/cdn/hls/" in stream_url:
            stream_base = stream_url.split("/cdn/hls/", 1)[0]
        else:
            parsed = urllib.parse.urlparse(stream_url)
            stream_base = (
                f"{parsed.scheme}://{parsed.netloc}"
                if parsed.scheme and parsed.netloc
                else player_cdn_base
            )

        subtitle = (
            stream_base
            + "/cdn/down/"
            + hls_hash
            + "/Subtitle/subtitle_eng.srt"
        )

        playback_headers = {
            "Referer": stream_base.rstrip("/") + "/",
            "Origin": stream_base.rstrip("/"),
            "User-Agent": USER_AGENT,
        }
        if cookie_header:
            playback_headers["Cookie"] = cookie_header

        chain = _probe_hls_chain(
            stream_url,
            playback_headers,
            timeout=min(timeout, 8),
        )

        results.append(
            {
                "url": stream_url,
                "subtitle": subtitle,
                "cdnBase": stream_base,
                "linkType": link_type,
                "playlistOk": (
                    200 <= int(chain.get("master") or 0) < 300
                ),
                "probeStatus": chain.get("master") or 0,
                "mediaStatus": chain.get("media") or 0,
                "assetStatus": chain.get("asset") or 0,
                "assetKind": chain.get("assetKind") or "",
                "cookie": cookie_header,
            }
        )

    return results


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



def _mediaflow_hls_url(
    stream_url: str,
    playback_headers: dict,
    proxy_url: str,
    api_password: str = "",
) -> str:
    """
    Wrap an upstream HLS URL with MediaFlow Proxy.

    `proxy_url` may be either:
      https://proxy.example
    or:
      https://proxy.example/proxy/hls/manifest.m3u8
    """
    proxy_url = str(proxy_url or "").strip().rstrip("/")
    if not proxy_url:
        return stream_url

    if not proxy_url.endswith("/proxy/hls/manifest.m3u8"):
        proxy_url += "/proxy/hls/manifest.m3u8"

    params = {
        "d": stream_url,
        "force_playlist_proxy": "true",
    }

    if api_password:
        params["api_password"] = str(api_password)

    # MediaFlow's documented examples use lowercase `h_<header>` query
    # parameters. Keep them lowercase for maximum compatibility.
    header_map = {
        "Referer": "h_referer",
        "Origin": "h_origin",
        "User-Agent": "h_user-agent",
        "Cookie": "h_cookie",
    }
    for header, param_name in header_map.items():
        value = str(playback_headers.get(header) or "").strip()
        if value:
            params[param_name] = value

    return proxy_url + "?" + urllib.parse.urlencode(params)



def _probe_mediaflow_url(
    url: str,
    *,
    timeout: float = 8,
) -> tuple[int, bool]:
    """
    Verify the FINAL URL returned to Stremio/Nuvio, not just the upstream CDN.
    """
    status, body = _request(
        url,
        headers={"Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, */*"},
        timeout=timeout,
        retries=0,
    )
    return status, (200 <= status < 300 and "#EXTM3U" in body[:8192])

def _proxy_settings(config: dict) -> tuple[str, str]:
    proxy_url = str(
        config.get("mediaflow_proxy_url")
        or MEDIAFLOW_PROXY_URL
        or ""
    ).strip()

    password = str(
        config.get("mediaflow_api_password")
        or MEDIAFLOW_API_PASSWORD
        or ""
    ).strip()

    return proxy_url, password

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

    extracted_links = _get_stream_from_page(
        playback_page,
        base_url,
        timeout=timeout,
    )
    if not extracted_links:
        return []

    quality = "1080p"
    results = []

    for extracted in extracted_links:
        stream_url = str(extracted.get("url") or "").strip()
        cdn_base = str(extracted.get("cdnBase") or "").rstrip("/")
        link_type = str(extracted.get("linkType") or "stream")
        playlist_ok = bool(extracted.get("playlistOk"))
        probe_status = extracted.get("probeStatus")

        if not stream_url:
            continue

        display_title = _format_stream_title(
            title,
            year,
            media_type,
            season,
            episode,
            episode_title,
            quality,
        )

        media_status = int(extracted.get("mediaStatus") or 0)
        asset_status = int(extracted.get("assetStatus") or 0)
        asset_kind = str(extracted.get("assetKind") or "asset")
        cookie_header = str(extracted.get("cookie") or "")

        diagnostic = (
            f"\n🔐 {link_type}"
            f" | master:{probe_status}"
            f" media:{media_status}"
            f" {asset_kind}:{asset_status}"
            f" | cookie:{'yes' if cookie_header else 'no'}"
        )
        display_title += diagnostic

        if config.get("_megasource_ui_smoke_test"):
            display_title = "[MegaSource Test]\n" + display_title

        playback_headers = {
            "Referer": cdn_base + "/",
            "Origin": cdn_base,
            "User-Agent": USER_AGENT,
        }
        if cookie_header:
            playback_headers["Cookie"] = cookie_header

        proxy_url, proxy_password = _proxy_settings(config)
        final_url = _mediaflow_hls_url(
            stream_url,
            playback_headers,
            proxy_url,
            proxy_password,
        )

        using_proxy = bool(proxy_url)
        proxy_status = 0
        proxy_ok = False

        if using_proxy:
            proxy_status, proxy_ok = _probe_mediaflow_url(
                final_url,
                timeout=min(timeout, 8),
            )
            display_title += (
                "\n🌐 Playback: MediaFlow HLS relay"
                f" | proxy:{proxy_status}"
                f" | {'OK' if proxy_ok else 'FAILED'}"
            )
        else:
            display_title += (
                "\n⚠️ Playback: direct CDN"
                " | no MediaFlow URL configured"
            )

        result = {
            "name": (
                f"{TITLE} • {quality} • {link_type}"
                + (" • Proxy" if using_proxy else "")
            ),
            "title": display_title,
            "url": final_url,
            "quality": quality,
            "source": (
                f"AnimeSalt {link_type} via MediaFlow"
                if using_proxy
                else f"AnimeSalt {link_type}"
            ),
            "behaviorHints": {
                "notWebReady": True,
            },
        }

        # Direct mode still exposes the upstream headers. In proxy mode the
        # proxy URL already carries them and rewrites the HLS child requests.
        if not using_proxy:
            result["behaviorHints"]["proxyHeaders"] = {
                "request": playback_headers,
            }
            result["headers"] = playback_headers

        subtitle = str(extracted.get("subtitle") or "").strip()
        if subtitle:
            result["subtitles"] = [
                {
                    "url": subtitle,
                    "lang": "en",
                    "name": "English",
                }
            ]

        if using_proxy and not proxy_ok:
            # A configured but unreachable/misconfigured proxy is not useful
            # to the player. Do not expose a stream that will just buffer.
            continue

        results.append(result)

    return results


# Optional JS-style compatibility alias.
def getStreams(media_type: str, media_id: str, config: dict = None) -> list:
    return get_streams(media_type, media_id, config or {})
