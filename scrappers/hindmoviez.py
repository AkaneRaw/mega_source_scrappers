"""
HindMovie provider port for MegaSource.

Converted from the supplied obfuscated JavaScript provider.

This port intentionally does NOT reproduce the original HShare signing /
"bypass" routine. It only resolves media URLs that are already publicly
present in:
  - HindMovie WordPress post content
  - publicly linked MvLink pages
  - publicly linked HCloud pages

MegaSource contract:
    TITLE, VERSION, DESCRIPTION
    get_streams(media_type, media_id, config) -> list[dict]

Config:
    base_url: str       Default: https://hindmovie.icu
    timeout: number     Default: 15
    title: str          Optional title override
    year: int           Optional year override
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

TITLE = "HindMovie"
VERSION = "1.0.0"
DESCRIPTION = "HindMovie public direct-link provider for MegaSource"

DEFAULT_BASE_URL = "https://hindmovie.icu"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}

DIRECT_EXT_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:m3u8|mp4|mkv|webm)(?:\?[^\s\"'<>]*)?",
    re.I,
)

WORKERS_RE = re.compile(
    r'https?://[^\s"\'<>]+\.workers\.dev[^\s"\'<>]*',
    re.I,
)

MVLINK_RE = re.compile(
    r'https?://mvlink\.blog/(?:web/)?\d+',
    re.I,
)

HCLOUD_RE = re.compile(
    r'https?://[^\s"\'<>]*hcloud\.ink[^\s"\'<>]*',
    re.I,
)


def _request(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: float = 15,
    retries: int = 1,
) -> tuple[int, str]:
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers=request_headers,
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return int(getattr(resp, "status", 200)), body

        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            if attempt >= retries:
                return int(exc.code), body

        except Exception:
            if attempt >= retries:
                return 0, ""

        time.sleep(0.35 * (2 ** attempt))

    return 0, ""


def _json_request(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: float = 15,
    retries: int = 1,
):
    status, body = _request(
        url,
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

    runtime = None
    match = re.search(r"(\d+)", str(meta.get("runtime") or ""))
    if match:
        runtime = int(match.group(1))

    return {
        "title": title,
        "year": year,
        "runtime": runtime,
        "videos": meta.get("videos") or [],
    }


def _decode_entities(value: str) -> str:
    return html.unescape(str(value or ""))


def _clean_title(value: str) -> str:
    value = _decode_entities(value).lower()

    patterns = [
        r"\bdownload\b",
        r"\b(dual audio|multi audio|hindi|english|tamil|telugu|malayalam|"
        r"korean|japanese|chinese|spanish|french|italian|german)\b",
        r"\b(480p|720p|1080p|2160p|4k|2k|hd|fhd|uhd)\b",
        r"\b(web-?dl|web-?dlrip|web-?rip|brrip|bdrip|bluray|blu-?ray|"
        r"hdtv|tvrip|dvdrip|camrip|hdrip)\b",
        r"\b(x264|x265|hevc|10bit|12bit|aac|ac3|dd5\.1|ddp5\.1|"
        r"atmos|dts)\b",
        r"\b(season|saison|staffel)\s*\d+(?:\s*(?:-|to)\s*\d+)?\b",
        r"\bs\d+(?:\s*(?:-|to)\s*\d+)?\b",
        r"\b(episode|episodes|ep)\s*\d+(?:\s*(?:-|to)\s*\d+)?"
        r"\s*(?:added|update|updated)?\b",
        r"\b(complete|all episodes|pack|batch)\b",
        r"\b(movie|film|part\s*\d+|vol\s*\d+|volume\s*\d+)\b",
        r"\b(unrated|extended|directors cut|uncut|18)\b",
        r"\b(19\d{2}|20\d{2})\b",
    ]

    for pattern in patterns:
        value = re.sub(pattern, " ", value, flags=re.I)

    value = re.sub(r"[^a-z0-9]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"^(the|a|an)\s+", "", value)
    return value


def _strict_match(
    wanted_title: str,
    wanted_year,
    candidate_title: str,
    candidate_year,
) -> bool:
    if not wanted_title or not candidate_title:
        return False

    if _clean_title(wanted_title) != _clean_title(candidate_title):
        return False

    if wanted_year and candidate_year:
        try:
            if abs(int(wanted_year) - int(candidate_year)) > 1:
                return False
        except Exception:
            pass

    return True


def _search_wp(
    query: str,
    base_url: str,
    *,
    timeout: float = 15,
) -> list:
    url = (
        base_url.rstrip("/")
        + "/wp-json/wp/v2/posts?"
        + urllib.parse.urlencode(
            {
                "search": query,
                "per_page": "100",
            }
        )
    )

    data = _json_request(
        url,
        headers={"Referer": base_url.rstrip("/") + "/"},
        timeout=timeout,
        retries=1,
    )

    if not isinstance(data, list):
        return []

    results = []

    for post in data:
        if not isinstance(post, dict):
            continue

        title_raw = (
            (post.get("title") or {}).get("rendered")
            if isinstance(post.get("title"), dict)
            else ""
        )
        title = re.sub(r"<[^>]+>", "", str(title_raw or "")).strip()

        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", title)

        content = (
            (post.get("content") or {}).get("rendered")
            if isinstance(post.get("content"), dict)
            else ""
        )

        results.append(
            {
                "id": post.get("id"),
                "title": _decode_entities(title),
                "year": int(year_match.group(1)) if year_match else None,
                "content": str(content or ""),
                "link": str(post.get("link") or ""),
            }
        )

    return results


def _pick_post(
    posts: list,
    title: str,
    year,
    imdb_id: str,
):
    for post in posts:
        if imdb_id and imdb_id in str(post.get("content") or ""):
            return post

    for post in posts:
        if _strict_match(
            title,
            year,
            post.get("title"),
            post.get("year"),
        ):
            return post

    return None


def _extract_season_html(
    content: str,
    season: Optional[int],
) -> str:
    if not content or season is None:
        return content

    markers = []
    regex = re.compile(
        r"(?:Season|Saison|Staffel)\s+0*(\d+)\b",
        re.I,
    )

    for match in regex.finditer(content):
        start = content.rfind("<", 0, match.start())
        if start < 0 or match.start() - start > 500:
            start = match.start()

        nearby = content[start:match.start() + 50].lower()
        if "download" in nearby or "episode" in nearby:
            continue

        markers.append(
            {
                "season": int(match.group(1)),
                "index": start,
            }
        )

    matches = [m for m in markers if m["season"] == int(season)]
    if not matches:
        return content

    start = matches[0]["index"]
    end = len(content)

    for marker in markers:
        if marker["index"] > start and marker["season"] != int(season):
            end = marker["index"]
            break

    return content[start:end]


def _quality_from_context(context: str) -> str:
    text = str(context or "")

    match = re.search(r"(2160|1080|720|480)\s*p", text, re.I)
    if match:
        return match.group(1) + "p"

    if re.search(r"\b(?:4k|uhd)\b", text, re.I):
        return "2160p"

    if re.search(r"\b(?:1440|2k)\b", text, re.I):
        return "1440p"

    return "HD"


def _extract_public_urls(page_html: str) -> list:
    decoded = html.unescape(str(page_html or ""))
    urls = []

    for regex in (DIRECT_EXT_RE, WORKERS_RE):
        for match in regex.finditer(decoded):
            url = match.group(0)
            url = url.rstrip(").,;")
            urls.append(url)

    # Some HTML contains escaped slashes.
    unescaped = decoded.replace("\\/", "/")
    if unescaped != decoded:
        for regex in (DIRECT_EXT_RE, WORKERS_RE):
            for match in regex.finditer(unescaped):
                urls.append(match.group(0).rstrip(").,;"))

    return _dedupe_strings(urls)


def _dedupe_strings(values: list) -> list:
    seen = set()
    result = []
    for value in values:
        value = str(value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _public_links_from_mvlink(
    mvlink_url: str,
    base_url: str,
    *,
    timeout: float = 15,
) -> list:
    """
    Public-only traversal.

    We intentionally do not call mvlink.blog's HindShare/HShare signing API.
    """
    status, page_html = _request(
        mvlink_url,
        headers={"Referer": base_url.rstrip("/") + "/"},
        timeout=timeout,
        retries=1,
    )
    if status != 200 or not page_html:
        return []

    results = _extract_public_urls(page_html)

    # If the MvLink page itself publicly exposes HCloud pages, those may be
    # inspected normally. No token-signing or protected-ID transformation is
    # performed here.
    for hcloud_url in _dedupe_strings(HCLOUD_RE.findall(html.unescape(page_html))):
        h_status, h_html = _request(
            hcloud_url,
            headers={"Referer": mvlink_url},
            timeout=timeout,
            retries=0,
        )
        if h_status == 200 and h_html:
            results.extend(_extract_public_urls(h_html))

    return _dedupe_strings(results)


def _probe_url(
    url: str,
    *,
    timeout: float = 8,
) -> tuple[int, bool, str]:
    lower = url.lower()

    if ".m3u8" in lower:
        status, body = _request(
            url,
            headers={"Referer": DEFAULT_BASE_URL + "/"},
            timeout=timeout,
            retries=0,
        )
        return (
            status,
            200 <= status < 300 and "#EXTM3U" in body[:8192],
            "HLS",
        )

    status, _ = _request(
        url,
        headers={
            "Referer": DEFAULT_BASE_URL + "/",
            "Range": "bytes=0-1023",
        },
        timeout=timeout,
        retries=0,
    )

    return status, status in (200, 206), "Direct"


def _audio_label(context: str) -> tuple[str, str]:
    lower = str(context or "").lower()

    if "dual" in lower or ("hindi" in lower and "english" in lower):
        return "Dual-Audio", "Hindi • English"
    if "multi" in lower:
        return "Multi-Audio", "Multilingual"
    if "tamil" in lower:
        return "Single-Audio", "Tamil"
    if "telugu" in lower:
        return "Single-Audio", "Telugu"
    if "english" in lower:
        return "Single-Audio", "English"

    return "Single-Audio", "Hindi"


def _make_stream(
    url: str,
    quality: str,
    title: str,
    year,
    media_type: str,
    season: Optional[int],
    episode: Optional[int],
    context: str,
    server_index: int,
    timeout: float,
) -> dict:
    audio_name, audio_display = _audio_label(context)

    lower = url.lower()
    if ".m3u8" in lower:
        fmt = "M3U8 / HLS"
    elif ".mp4" in lower:
        fmt = "MP4"
    elif ".mkv" in lower:
        fmt = "MKV"
    else:
        fmt = "Direct"

    runtime = "45 min" if media_type == "series" else "N/A"

    probe_status, probe_ok, probe_kind = _probe_url(
        url,
        timeout=min(timeout, 8),
    )

    if media_type == "series":
        line1 = (
            f"🎬 {title} [S{int(season or 1):02d}E{int(episode or 1):02d}]"
            + (f" ({year})" if year else "")
        )
    else:
        line1 = f"🎬 {title}" + (f" - {year}" if year else "")

    display = "\n".join(
        [
            line1,
            f"💎 {quality} | 🔊 {audio_display} | 🗃️ Server {server_index}",
            f"🎞️ {fmt} | ⏱️ {runtime} | 📌 WEB-DL",
            f"🧪 {probe_kind} probe: {probe_status} | {'OK' if probe_ok else 'FAILED'}",
        ]
    )

    return {
        "name": f"🔵 HindMovie | {quality} | {audio_name}",
        "title": display,
        "url": url,
        "quality": quality,
        "source": f"HindMovie Server {server_index}",
        "behaviorHints": {
            "notWebReady": True,
        },
    }


def get_streams(
    media_type: str,
    media_id: str,
    config: dict,
) -> list:
    config = config or {}

    media_type = (
        "series"
        if str(media_type or "").lower() in ("series", "tv")
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

    title_override = str(config.get("title") or "").strip()

    if title_override:
        try:
            year = int(config.get("year") or 0) or None
        except Exception:
            year = None

        details = {
            "title": title_override,
            "year": year,
            "runtime": None,
            "videos": [],
        }
    else:
        details = _cinemeta_details(
            base_id,
            media_type,
            timeout=timeout,
        )
        if not details:
            return []

    title = str(details.get("title") or "").strip()
    year = details.get("year")
    if not title:
        return []

    posts = []

    if str(base_id).lower().startswith("tt"):
        posts = _search_wp(
            base_id,
            base_url,
            timeout=timeout,
        )

    if not posts:
        posts = _search_wp(
            title,
            base_url,
            timeout=timeout,
        )

    post = _pick_post(
        posts,
        title,
        year,
        str(base_id) if str(base_id).lower().startswith("tt") else "",
    )
    if not post:
        return []

    content = str(post.get("content") or "")
    if media_type == "series":
        content = _extract_season_html(content, season)

    candidates = []

    # 1) Direct public media already present in the HindMovie post.
    for url in _extract_public_urls(content):
        candidates.append(
            {
                "url": url,
                "quality": _quality_from_context(content),
                "context": post.get("title") or title,
            }
        )

    # 2) Public media exposed directly by linked MvLink pages.
    mvlinks = _dedupe_strings(MVLINK_RE.findall(html.unescape(content)))

    for mvlink_url in mvlinks:
        quality = "HD"
        pos = content.find(mvlink_url)
        if pos >= 0:
            context = content[max(0, pos - 500):pos]
            quality = _quality_from_context(context)

        for url in _public_links_from_mvlink(
            mvlink_url,
            base_url,
            timeout=timeout,
        ):
            candidates.append(
                {
                    "url": url,
                    "quality": quality,
                    "context": post.get("title") or title,
                }
            )

    # Deduplicate and skip low-quality 480p like the original provider.
    seen = set()
    streams = []

    for item in candidates:
        url = str(item.get("url") or "").strip()
        quality = str(item.get("quality") or "HD")

        if not url or url in seen or quality == "480p":
            continue

        seen.add(url)

        streams.append(
            _make_stream(
                url,
                quality,
                title,
                year,
                media_type,
                season,
                episode,
                str(item.get("context") or ""),
                len(streams) + 1,
                timeout,
            )
        )

    rank = {
        "2160p": 5,
        "1440p": 4,
        "1080p": 3,
        "720p": 2,
        "HD": 1,
    }

    streams.sort(
        key=lambda item: rank.get(
            str(item.get("quality") or ""),
            0,
        ),
        reverse=True,
    )

    return streams


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
