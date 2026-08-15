"""
VegaMovies provider port for MegaSource.

Converted from the supplied obfuscated JavaScript provider.

This port follows the provider's public request flow:
    IMDb/Stremio ID
      -> Cinemeta metadata
      -> dynamic VegaMovies / HubCloud / vCloud domains
      -> VegaMovies search API
      -> WordPress post content
      -> NexDrive / GenXFM / FastDL / HubCloud / vCloud pages
      -> direct public Worker/FSL media links

It intentionally does not attempt excluded third-party host bypasses such as
FilePress, GDTot, DropGalaxy, GDFlix, or GDLink.

MegaSource contract:
    TITLE, VERSION, DESCRIPTION
    get_streams(media_type, media_id, config) -> list[dict]

Config:
    timeout: number          Default: 7
    max_post_links: int      Default: 8
    max_streams: int         Default: 6
    validate_streams: bool   Default: False
    base_url: str            Optional VegaMovies override
    hubcloud_url: str        Optional HubCloud override
    vcloud_url: str          Optional vCloud override
    domains_json_url: str    Optional domain-feed override
    title: str               Optional metadata override
    year: int                Optional metadata override
"""

from __future__ import annotations

import base64
import html
from html.parser import HTMLParser
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

TITLE = "VegaMovies"
VERSION = "1.0.0"
DESCRIPTION = "VegaMovies public HubCloud/vCloud direct-link provider"

DEFAULT_BASE_URL = "https://vegamovies.mq"
DEFAULT_HUBCLOUD_URL = "https://hubcloud.foo"
DEFAULT_VCLOUD_URL = "https://vcloud.zip"

DOMAINS_JSON_URL = (
    "https://raw.githubusercontent.com/"
    "SaurabhKaperwan/Utils/refs/heads/main/urls.json"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/119.0.0.0 Mobile Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "identity",
}

EXCLUDED_LABELS = (
    "filepress",
    "gdtot",
    "dropgalaxy",
    "gdflix",
    "gdlink",
    "10gbps",
    "telegram",
)

MEGASOURCE_TEST_MEDIA_TYPE = "movie"
MEGASOURCE_TEST_MEDIA_ID = "tt0111161"
MEGASOURCE_TEST_TITLE = "The Shawshank Redemption"
MEGASOURCE_TEST_YEAR = 1994


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._current = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        data = dict(attrs)
        self._current = {
            "href": str(data.get("href") or ""),
            "id": str(data.get("id") or ""),
            "class": str(data.get("class") or ""),
        }
        self._text = []

    def handle_data(self, data):
        if self._current is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._current is not None:
            item = dict(self._current)
            item["text"] = " ".join(self._text).strip()
            self.links.append(item)
            self._current = None
            self._text = []


def _parse_links(page_html: str) -> list:
    parser = _LinkParser()
    try:
        parser.feed(str(page_html or ""))
    except Exception:
        pass
    return parser.links


def _request(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: float = 7,
    retries: int = 0,
) -> tuple[int, str, str, dict]:
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
                final_url = str(getattr(resp, "url", url) or url)
                return (
                    int(getattr(resp, "status", 200)),
                    body,
                    final_url,
                    dict(resp.headers.items()),
                )

        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""

            if attempt >= retries:
                return (
                    int(exc.code),
                    body,
                    url,
                    dict(exc.headers.items()) if exc.headers else {},
                )

        except Exception:
            if attempt >= retries:
                return 0, "", url, {}

        time.sleep(0.25 * (attempt + 1))

    return 0, "", url, {}


def _json_request(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: float = 7,
):
    status, body, _, _ = _request(
        url,
        headers=headers,
        timeout=timeout,
        retries=0,
    )

    if not (200 <= status < 300) or not body:
        return None

    try:
        return json.loads(body)
    except Exception:
        return None


def _mobile_headers(
    base_url: str,
    referer: Optional[str] = None,
) -> dict:
    return {
        "User-Agent": MOBILE_USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Referer": referer or base_url.rstrip("/") + "/",
    }


def _origin(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return url


def _absolute_url(value: str, base_url: str) -> str:
    value = html.unescape(str(value or "").strip())

    if value.startswith("https://") or value.startswith("http://"):
        return value

    if value.startswith("//"):
        return "https:" + value

    return urllib.parse.urljoin(base_url.rstrip("/") + "/", value)


def _append_cache_buster(url: str) -> str:
    minute = 1 + time.localtime().tm_min
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}s={minute}"


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
    timeout: float = 7,
) -> Optional[dict]:
    if not re.fullmatch(r"tt\d+", str(imdb_id or ""), re.I):
        return None

    meta_type = "series" if media_type == "series" else "movie"

    url = (
        f"https://v3-cinemeta.strem.io/meta/{meta_type}/"
        f"{urllib.parse.quote(str(imdb_id))}.json"
    )

    data = _json_request(url, timeout=timeout)
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

    return {
        "title": title,
        "year": year,
        "imdb_id": imdb_id,
        "videos": meta.get("videos") or [],
    }


def _refresh_domains(
    config: dict,
    *,
    timeout: float = 5,
) -> dict:
    base_url = str(
        config.get("base_url")
        or DEFAULT_BASE_URL
    ).strip().rstrip("/")

    hubcloud = str(
        config.get("hubcloud_url")
        or DEFAULT_HUBCLOUD_URL
    ).strip().rstrip("/")

    vcloud = str(
        config.get("vcloud_url")
        or DEFAULT_VCLOUD_URL
    ).strip().rstrip("/")

    domains_url = str(
        config.get("domains_json_url")
        or DOMAINS_JSON_URL
    ).strip()

    data = _json_request(
        domains_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        },
        timeout=min(timeout, 5),
    )

    if isinstance(data, dict):
        if not config.get("base_url") and data.get("vegamovies"):
            base_url = str(data["vegamovies"]).strip().rstrip("/")

        if not config.get("hubcloud_url") and data.get("hubcloud"):
            hubcloud = str(data["hubcloud"]).strip().rstrip("/")

        if not config.get("vcloud_url") and data.get("vcloud"):
            vcloud = str(data["vcloud"]).strip().rstrip("/")

    return {
        "base_url": base_url,
        "hubcloud": hubcloud,
        "vcloud": vcloud,
    }


def _normalize_title(value: str) -> str:
    value = html.unescape(str(value or "")).lower()
    value = re.sub(r"\bdownload\b", " ", value)
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _strict_match(
    wanted_title: str,
    wanted_year,
    candidate_title: str,
    candidate_year,
) -> bool:
    if not candidate_title:
        return False

    wanted = _normalize_title(wanted_title)
    candidate = _normalize_title(candidate_title)

    if not wanted or not (
        wanted in candidate
        or candidate in wanted
    ):
        return False

    if wanted_year and candidate_year:
        try:
            if abs(int(wanted_year) - int(candidate_year)) > 1:
                return False
        except Exception:
            pass

    return True


def _search(
    query: str,
    base_url: str,
    *,
    timeout: float = 7,
) -> list:
    if not query:
        return []

    url = (
        base_url.rstrip("/")
        + "/search.php?"
        + urllib.parse.urlencode(
            {
                "q": query,
                "page": "1",
                "per_page": "15",
            }
        )
    )

    data = _json_request(
        url,
        headers=_mobile_headers(base_url),
        timeout=timeout,
    )

    if not isinstance(data, dict):
        return []

    hits = data.get("hits") or []
    if not isinstance(hits, list):
        return []

    results = []

    for hit in hits:
        if not isinstance(hit, dict):
            continue

        document = hit.get("document") or {}
        if not isinstance(document, dict):
            continue

        raw_title = str(
            document.get("post_title")
            or document.get("title")
            or ""
        )

        title = re.sub(
            r"Download\s*",
            "",
            html.unescape(raw_title),
            flags=re.I,
        ).strip()

        year = None
        categories = document.get("category")

        if isinstance(categories, list):
            for category in categories:
                value = str(category or "").strip()
                if re.fullmatch(r"(?:19|20)\d{2}", value):
                    year = int(value)
                    break

        if not year:
            match = re.search(
                r"\b(?:19|20)\d{2}\b",
                raw_title,
            )
            if match:
                year = int(match.group(0))

        results.append(
            {
                "post_id": str(document.get("id") or ""),
                "title": title,
                "permalink": str(document.get("permalink") or ""),
                "imdb_id": str(document.get("imdb_id") or ""),
                "year": year,
            }
        )

    return results


def _series_title_matches_season(title: str, season: int) -> bool:
    title = str(title or "")

    range_match = re.search(
        r"(?:s|season|staffel|saison)\s*0*(\d+)\s*"
        r"(?:-|–|to|and|&|&#)\s*0*(\d+)\b",
        title,
        re.I,
    )

    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        return start <= int(season) <= end

    return bool(
        re.search(
            rf"(?:s|season|staffel|saison)\s*0*{int(season)}\b",
            title,
            re.I,
        )
    )


def _choose_search_result(
    results: list,
    *,
    imdb_id: str,
    title: str,
    year,
    media_type: str,
    season: Optional[int],
):
    if imdb_id:
        for item in results:
            if item.get("imdb_id") == imdb_id:
                if media_type != "series" or season is None:
                    return item

                if _series_title_matches_season(
                    item.get("title") or "",
                    int(season),
                ):
                    return item

    for item in results:
        if _strict_match(
            title,
            year,
            item.get("title"),
            item.get("year"),
        ):
            if (
                media_type == "series"
                and season is not None
                and re.search(
                    r"(?:s|season|staffel|saison)\s*\d+",
                    str(item.get("title") or ""),
                    re.I,
                )
                and not _series_title_matches_season(
                    item.get("title") or "",
                    int(season),
                )
            ):
                continue

            return item

    return None


def _strip_tags(value: str) -> str:
    return re.sub(
        r"<[^>]+>",
        " ",
        html.unescape(str(value or "")),
    )


def _extract_html_title(page_html: str) -> str:
    match = re.search(
        r"<title[^>]*>([\s\S]*?)</title>",
        str(page_html or ""),
        re.I,
    )

    return (
        re.sub(r"\s+", " ", _strip_tags(match.group(1))).strip()
        if match
        else ""
    )


def _fetch_post(
    post_id: str,
    permalink: str,
    base_url: str,
    *,
    timeout: float = 7,
):
    if post_id:
        wp_url = (
            base_url.rstrip("/")
            + "/wp-json/wp/v2/posts/"
            + urllib.parse.quote(str(post_id))
        )

        data = _json_request(
            wp_url,
            headers=_mobile_headers(base_url),
            timeout=timeout,
        )

        if isinstance(data, dict):
            content = data.get("content") or {}
            rendered = (
                content.get("rendered")
                if isinstance(content, dict)
                else ""
            )

            if rendered and re.search(
                r"nexdrive|vcloud|hubcloud|fastdl|genxfm",
                str(rendered),
                re.I,
            ):
                title_data = data.get("title") or {}
                post_title = (
                    title_data.get("rendered")
                    if isinstance(title_data, dict)
                    else ""
                )

                return {
                    "title": re.sub(
                        r"Download\s*",
                        "",
                        _strip_tags(post_title),
                        flags=re.I,
                    ).strip(),
                    "html": str(rendered),
                }

    fallback_url = (
        _absolute_url(permalink, base_url)
        if permalink
        else base_url.rstrip("/") + "/?p=" + str(post_id)
    )

    status, page_html, _, _ = _request(
        fallback_url,
        headers=_mobile_headers(base_url),
        timeout=timeout,
        retries=0,
    )

    if status != 200 or not page_html:
        return None

    # Keep the complete HTML in fallback mode. Link extraction below is
    # intentionally narrow and only considers known resolver hosts.
    return {
        "title": _extract_html_title(page_html),
        "html": page_html,
    }


def _extract_season_html(
    content: str,
    season: Optional[int],
) -> str:
    if not content or season is None:
        return content

    before_comments = re.split(
        r'id=["\']comments["\']|class=["\'][^"\']*comments-area',
        content,
        maxsplit=1,
        flags=re.I,
    )[0]

    markers = []

    regex = re.compile(
        r"(?:Season|Saison|Staffel)\s+0*(\d+)\b"
        r"(?!\s*(?:-|–|to|and|&|&#))",
        re.I,
    )

    for match in regex.finditer(before_comments):
        h_pos = before_comments.rfind("<h", 0, match.start())
        strong_pos = before_comments.rfind("<strong", 0, match.start())
        start = max(h_pos, strong_pos)

        if start < 0 or match.start() - start > 500:
            start = match.start()

        nearby = before_comments[start:match.start() + 50].lower()

        if "download" in nearby or "episode" in nearby:
            continue

        markers.append(
            {
                "season": int(match.group(1)),
                "index": start,
            }
        )

    matches = [
        item
        for item in markers
        if item["season"] == int(season)
    ]

    if not matches:
        return before_comments

    start = matches[0]["index"]
    end = len(before_comments)

    for item in markers:
        if item["index"] > start and item["season"] != int(season):
            end = item["index"]
            break

    return before_comments[start:end]


def _parse_quality(value: str) -> str:
    text = str(value or "")

    match = re.search(
        r"(2160|1080|720|480)\s*p",
        text,
        re.I,
    )
    if match:
        return match.group(1) + "p"

    if re.search(r"\b(?:4K|UHD)\b", text, re.I):
        return "2160p"

    if re.search(r"\b(?:1440|2K)\b", text, re.I):
        return "1440p"

    return "HD"


def _quality_near_link(content: str, href: str) -> str:
    position = content.find(href)
    if position < 0:
        return "HD"

    nearby = content[max(0, position - 3000):position]

    matches = list(
        re.finditer(
            r"(?:^|>|\s)(\d{3,4}p|4K|UHD|HDR)(?:<|\s|$)",
            nearby,
            re.I,
        )
    )

    if matches:
        return _parse_quality(matches[-1].group(1))

    return _parse_quality(nearby[-700:])


def _extract_post_links(
    content: str,
    base_url: str,
) -> list:
    results = []
    seen = set()

    for link in _parse_links(content):
        href = str(link.get("href") or "").strip()
        label = str(link.get("text") or "").strip()

        if not href:
            continue

        lower_href = href.lower()
        lower_label = label.lower()

        if not any(
            token in lower_href
            for token in (
                "nexdrive",
                "genxfm",
                "fastdl",
                "vcloud",
                "hubcloud",
            )
        ):
            continue

        if any(value in lower_label for value in EXCLUDED_LABELS):
            continue

        absolute = _absolute_url(href, base_url)
        if absolute in seen:
            continue
        seen.add(absolute)

        quality = _quality_near_link(content, href)

        if quality == "480p":
            continue

        results.append(
            {
                "href": absolute,
                "quality": quality,
                "label": label or "Download",
            }
        )

    return results


def _rewrite_resolver_domain(
    url: str,
    domains: dict,
) -> str:
    lower = url.lower()

    replacement = None

    if "hubcloud" in lower:
        replacement = domains["hubcloud"]
    elif "vcloud" in lower:
        replacement = domains["vcloud"]

    if not replacement:
        return url

    source_origin = _origin(url)
    target_origin = replacement.rstrip("/")

    if source_origin != target_origin:
        return url.replace(source_origin, target_origin, 1)

    return url


def _double_b64_decode(value: str) -> Optional[str]:
    try:
        first = base64.b64decode(value + "===")
        second = base64.b64decode(first + b"===")
        return second.decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_script_url(page_html: str) -> Optional[str]:
    double = re.search(
        r"var\s+url\s*=\s*atob\(atob\(['\"]([^'\"]+)['\"]\)\)",
        page_html,
        re.I,
    )

    if double:
        decoded = _double_b64_decode(double.group(1))
        if decoded:
            return html.unescape(decoded.strip())

    simple = re.search(
        r"var\s+url\s*=\s*['\"]([^'\"]+)['\"]",
        page_html,
        re.I,
    )

    if simple:
        return html.unescape(simple.group(1).strip())

    return None


def _episode_title_matches(
    page_title: str,
    season: Optional[int],
    episode: Optional[int],
) -> bool:
    if season is None and episode is None:
        return True

    ep_match = re.search(
        r"[.\s_\-](?:S|Season)\s*0*(\d{1,2})"
        r"[.\s_\-]*(?:E|Ep|Episode)\s*0*(\d{1,2})[.\s_\-]",
        page_title,
        re.I,
    )

    if ep_match:
        found_season = int(ep_match.group(1))
        found_episode = int(ep_match.group(2))

        if season is not None and found_season != int(season):
            return False

        if episode is not None and found_episode != int(episode):
            return False

        return True

    season_match = re.search(
        r"[.\s_\-](?:S|Season)\s*0*(\d{1,2})[.\s_\-]",
        page_title,
        re.I,
    )

    if season_match and season is not None:
        return int(season_match.group(1)) == int(season)

    return True


def _direct_candidates_from_page(
    page_html: str,
    page_url: str,
    quality: str,
) -> list:
    candidates = []

    script_url = _extract_script_url(page_html)

    if script_url:
        absolute = _absolute_url(script_url, page_url)

        if ".workers.dev" in absolute.lower():
            candidates.append(
                {
                    "url": _append_cache_buster(absolute),
                    "label": "Worker Server",
                    "quality": quality,
                    "referer": page_url,
                }
            )

    for link in _parse_links(page_html):
        href = str(link.get("href") or "").strip()
        text = str(link.get("text") or "").strip()
        lower_text = text.lower()
        lower_href = href.lower()

        if not href or href == "#":
            continue

        if ".zip" in lower_href:
            continue

        if any(value in lower_text for value in EXCLUDED_LABELS):
            continue

        absolute = _absolute_url(href, page_url)

        if "fslv2" in lower_text:
            candidates.append(
                {
                    "url": absolute,
                    "label": text or "FSLv2",
                    "quality": quality,
                    "referer": page_url,
                }
            )
            continue

        if "fsl" in lower_text:
            candidates.append(
                {
                    "url": _append_cache_buster(absolute),
                    "label": text or "FSL",
                    "quality": quality,
                    "referer": page_url,
                }
            )
            continue

        if "worker" in lower_text or ".workers.dev" in absolute.lower():
            candidates.append(
                {
                    "url": _append_cache_buster(absolute),
                    "label": text or "Worker",
                    "quality": quality,
                    "referer": page_url,
                }
            )
            continue

        if re.search(
            r"\.(?:m3u8|mp4|mkv|webm)(?:\?|$)",
            absolute,
            re.I,
        ):
            candidates.append(
                {
                    "url": absolute,
                    "label": text or "Direct",
                    "quality": quality,
                    "referer": page_url,
                }
            )

    return candidates


def _next_download_page(
    page_html: str,
    page_url: str,
) -> Optional[str]:
    links = _parse_links(page_html)

    for link in links:
        href = str(link.get("href") or "").strip()
        link_id = str(link.get("id") or "").lower()

        if not href:
            continue

        lower = href.lower()

        if link_id == "download":
            return _absolute_url(href, page_url)

        if (
            "hubcloud.php" in lower
            or "token" in lower
            or re.search(r"(?:^|/)dl(?:/|$|\?)", lower)
        ):
            return _absolute_url(href, page_url)

    for link in links:
        href = str(link.get("href") or "").strip()

        if (
            href
            and "vcloud" in href.lower()
            and href != page_url
            and "/api/" not in href.lower()
        ):
            return _absolute_url(href, page_url)

    return None


def _resolve_vcloud(
    input_url: str,
    *,
    referer: str,
    quality: str,
    season: Optional[int],
    episode: Optional[int],
    domains: dict,
    base_url: str,
    timeout: float,
) -> list:
    page_url = _rewrite_resolver_domain(input_url, domains)

    headers = _mobile_headers(
        base_url,
        referer=referer or base_url.rstrip("/") + "/",
    )
    headers["Cookie"] = "xla=s4t"

    status, page_html, final_url, _ = _request(
        page_url,
        headers=headers,
        timeout=timeout,
        retries=0,
    )

    if status != 200 or not page_html:
        return []

    page_url = final_url or page_url
    page_title = _extract_html_title(page_html)

    if not _episode_title_matches(
        page_title,
        season,
        episode,
    ):
        return []

    header_match = re.search(
        r'<div[^>]*class=["\'][^"\']*card-header[^"\']*["\'][^>]*>'
        r'([\s\S]*?)</div>',
        page_html,
        re.I,
    )

    page_quality = (
        _parse_quality(_strip_tags(header_match.group(1)))
        if header_match
        else quality
    )

    direct = _direct_candidates_from_page(
        page_html,
        page_url,
        page_quality or quality,
    )

    if direct:
        return direct

    next_page = _next_download_page(
        page_html,
        page_url,
    )

    if not next_page:
        return []

    # At most one additional public page hop, matching the original
    # HubCloud/vCloud flow while avoiding unbounded redirect chains.
    second_headers = _mobile_headers(
        base_url,
        referer=page_url,
    )
    second_headers["Cookie"] = "xla=s4t"

    status, second_html, second_final, _ = _request(
        next_page,
        headers=second_headers,
        timeout=timeout,
        retries=0,
    )

    if status != 200 or not second_html:
        return []

    second_url = second_final or next_page

    second_header_match = re.search(
        r'<div[^>]*class=["\'][^"\']*card-header[^"\']*["\'][^>]*>'
        r'([\s\S]*?)</div>',
        second_html,
        re.I,
    )

    second_quality = (
        _parse_quality(
            _strip_tags(second_header_match.group(1))
        )
        if second_header_match
        else page_quality
    )

    return _direct_candidates_from_page(
        second_html,
        second_url,
        second_quality or quality,
    )


def _resolve_outer_page(
    url: str,
    *,
    referer: str,
    quality: str,
    season: Optional[int],
    episode: Optional[int],
    domains: dict,
    base_url: str,
    timeout: float,
) -> list:
    lower = url.lower()

    if "vcloud" in lower or "hubcloud" in lower:
        return _resolve_vcloud(
            url,
            referer=referer,
            quality=quality,
            season=season,
            episode=episode,
            domains=domains,
            base_url=base_url,
            timeout=timeout,
        )

    if not any(
        token in lower
        for token in ("nexdrive", "genxfm", "fastdl")
    ):
        return []

    status, page_html, final_url, _ = _request(
        url,
        headers=_mobile_headers(base_url, referer=referer),
        timeout=timeout,
        retries=0,
    )

    if status != 200 or not page_html:
        return []

    current_url = final_url or url
    resolver_links = []

    for link in _parse_links(page_html):
        href = str(link.get("href") or "").strip()

        if not href:
            continue

        absolute = _absolute_url(href, current_url)
        lower_absolute = absolute.lower()

        if not (
            "vcloud" in lower_absolute
            or "hubcloud" in lower_absolute
        ):
            continue

        # Some NexDrive pages expose an intermediate API page. Fetch it
        # normally and use the public button URL it returns.
        if "/api/index.php?link=" in lower_absolute:
            api_status, api_html, api_final, _ = _request(
                absolute,
                headers=_mobile_headers(
                    base_url,
                    referer=current_url,
                ),
                timeout=timeout,
                retries=0,
            )

            if api_status == 200 and api_html:
                for api_link in _parse_links(api_html):
                    api_href = str(
                        api_link.get("href") or ""
                    ).strip()

                    if not api_href:
                        continue

                    api_text = str(
                        api_link.get("text") or ""
                    ).lower()

                    api_class = str(
                        api_link.get("class") or ""
                    ).lower()

                    if (
                        "btn-success" in api_class
                        or "btn" in api_class
                        or "download" in api_text
                    ):
                        resolver_links.append(
                            _absolute_url(
                                api_href,
                                api_final or absolute,
                            )
                        )
                        break

            continue

        resolver_links.append(absolute)

    results = []

    for resolver_url in _dedupe_strings(resolver_links)[:5]:
        results.extend(
            _resolve_vcloud(
                resolver_url,
                referer=current_url,
                quality=quality,
                season=season,
                episode=episode,
                domains=domains,
                base_url=base_url,
                timeout=timeout,
            )
        )

        if results:
            break

    return results


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


def _parse_size(label: str) -> tuple[str, float]:
    match = re.search(
        r"\[\s*(\d+(?:\.\d+)?)\s*([MG]B)\s*\]",
        str(label or ""),
        re.I,
    )

    if not match:
        return "N/A", 0.0

    number = float(match.group(1))
    unit = match.group(2).upper()

    weight = number * 1024 if unit == "GB" else number

    return f"{number:g} {unit}", weight


def _audio_from_label(label: str):
    lower = str(label or "").lower()

    if re.search(r"dual|hindi-?eng|eng-?hin", lower):
        return "Dual-Audio", "English 🇺🇸 • Hindi 🇮🇳"

    languages = []

    if re.search(r"hindi|\bhin\b", lower):
        languages.append("Hindi 🇮🇳")

    if re.search(r"english|\beng\b", lower):
        languages.append("English 🇺🇸")

    if not languages:
        languages.append("English 🇺🇸")

    return "Single Audio", " • ".join(languages)


def _codec_from_label(label: str) -> str:
    lower = str(label or "").lower()

    if "hevc" in lower:
        return "HEVC"

    if "x265" in lower or "h265" in lower:
        return "H.265"

    return "H.264"


def _audio_codec_from_label(label: str) -> str:
    text = str(label or "")

    match = re.search(
        r"(TrueHD\s*7\.1|DDP\s*7\.1|DDP\s*5\.1|DD\s*5\.1|5\.1|AAC)",
        text,
        re.I,
    )

    if match:
        value = re.sub(r"\s+", "", match.group(1).upper())
        if value == "DDP5.1":
            return "DDP5.1"
        if "TRUEHD" in value:
            return "TrueHD 7.1"
        return value

    if re.search(r"dolby\s*digital|\bdd\b", text, re.I):
        return "Dolby Digital"

    return "AAC"


def _source_kind(url: str) -> str:
    lower = url.lower()

    if ".m3u8" in lower:
        return "M3U8"

    if ".mp4" in lower:
        return "MP4"

    if ".mkv" in lower:
        return "MKV"

    return "Direct"


def _validate_direct_media(
    url: str,
    referer: str,
    *,
    timeout: float = 2.5,
) -> bool:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Accept": "*/*",
        "Range": "bytes=0-4095",
    }

    try:
        req = urllib.request.Request(
            url,
            headers=headers,
            method="GET",
        )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4096)
            content_type = str(
                resp.headers.get("Content-Type") or ""
            ).lower()

            if (
                "text/html" in content_type
                or "application/json" in content_type
            ):
                return False

            text = raw.decode("utf-8", errors="ignore").lstrip()

            if text.lower().startswith(("<!doctype html", "<html", "{")):
                return False

            if "#EXTM3U" in text:
                return True

            if len(raw) >= 8 and raw[4:8] == b"ftyp":
                return True

            if raw[:4] == b"\x1a\x45\xdf\xa3":
                return True

            if content_type.startswith("video/"):
                return True

            if "application/octet-stream" in content_type:
                return True

            # Worker/FSL links commonly omit file extensions while still
            # serving binary media.
            return len(raw) > 0

    except urllib.error.HTTPError as exc:
        return int(exc.code) in (200, 206)
    except Exception:
        return False


def _make_stream(
    candidate: dict,
    *,
    title: str,
    year,
    media_type: str,
    season: Optional[int],
    episode: Optional[int],
) -> dict:
    url = str(candidate.get("url") or "")
    quality = str(candidate.get("quality") or "HD")
    label = str(candidate.get("label") or "")
    referer = str(candidate.get("referer") or "")

    size, size_weight = _parse_size(label)
    audio_name, languages = _audio_from_label(label)
    codec = _codec_from_label(label)
    audio_codec = _audio_codec_from_label(label)
    fmt = _source_kind(url)

    display_title = title

    if media_type == "series":
        display_title += (
            f" - S{int(season or 1)} E{int(episode or 1)}"
        )
    elif year:
        display_title += f" - ({year})"

    provider = (
        "HubCloud"
        if "hubcloud" in referer.lower()
        else "vCloud"
        if "vcloud" in referer.lower()
        else "Direct"
    )

    lines = [
        f"🎬 {display_title}",
        f"💎 {quality} | 🗣️ {languages} | 💾 {size}",
        f"🎞️ {fmt} | 🎧 {audio_codec} | ⚡ {codec}",
        f"🔗 {provider} | ☁️ VegaMovies",
    ]

    headers = {
        "Referer": referer,
        "User-Agent": USER_AGENT,
    }

    return {
        "name": f"VegaMovies | {quality} | {audio_name}",
        "title": "\n".join(lines),
        "url": url,
        "quality": quality,
        "source": f"VegaMovies {provider}",
        "behaviorHints": {
            "notWebReady": True,
            "proxyHeaders": {
                "request": headers,
            },
        },
        "headers": headers,
        "_res_weight": (
            3 if quality == "2160p"
            else 2 if quality == "1080p"
            else 1
        ),
        "_size_weight": size_weight,
    }


def get_streams(
    media_type: str,
    media_id: str,
    config: dict,
) -> list:
    config = config or {}

    raw_type = str(media_type or "").lower()
    raw_id = str(media_id or "")

    media_type = (
        "series"
        if raw_type in ("series", "tv")
        else "movie"
    )

    base_id, season, episode = _parse_media_id(raw_id)

    if media_type == "series":
        season = int(season or 1)
        episode = int(episode or 1)
    else:
        season = None
        episode = None

    try:
        timeout = float(config.get("timeout", 7))
    except Exception:
        timeout = 7

    try:
        max_post_links = max(
            1,
            min(15, int(config.get("max_post_links", 8))),
        )
    except Exception:
        max_post_links = 8

    try:
        max_streams = max(
            1,
            min(10, int(config.get("max_streams", 6))),
        )
    except Exception:
        max_streams = 6

    validate_streams = bool(
        config.get("validate_streams", False)
    )

    domains = _refresh_domains(
        config,
        timeout=min(timeout, 5),
    )

    base_url = domains["base_url"]

    ui_smoke_test = (
        raw_type == MEGASOURCE_TEST_MEDIA_TYPE
        and raw_id == MEGASOURCE_TEST_MEDIA_ID
        and not config
    )

    title_override = str(config.get("title") or "").strip()

    if ui_smoke_test:
        details = {
            "title": MEGASOURCE_TEST_TITLE,
            "year": MEGASOURCE_TEST_YEAR,
            "imdb_id": MEGASOURCE_TEST_MEDIA_ID,
            "videos": [],
        }
    elif title_override:
        try:
            override_year = int(
                config.get("year")
                or 0
            ) or None
        except Exception:
            override_year = None

        details = {
            "title": title_override,
            "year": override_year,
            "imdb_id": (
                base_id
                if re.fullmatch(r"tt\d+", base_id, re.I)
                else ""
            ),
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
    imdb_id = str(details.get("imdb_id") or "")

    if not title:
        return []

    results = []

    if imdb_id:
        results = _search(
            imdb_id,
            base_url,
            timeout=timeout,
        )

    exact_imdb = any(
        item.get("imdb_id") == imdb_id
        for item in results
    )

    if not results or not exact_imdb:
        if media_type == "series" and season is not None:
            query = f"{title} season {season}"
        else:
            query = title + (f" {year}" if year else "")

        results = _search(
            query,
            base_url,
            timeout=timeout,
        )

        if (
            not results
            and media_type == "series"
            and season is not None
        ):
            results = _search(
                title + (f" {year}" if year else ""),
                base_url,
                timeout=timeout,
            )

    if not results:
        return []

    selected = _choose_search_result(
        results,
        imdb_id=imdb_id,
        title=title,
        year=year,
        media_type=media_type,
        season=season,
    )

    if not selected or not selected.get("post_id"):
        return []

    post = _fetch_post(
        selected["post_id"],
        selected.get("permalink") or "",
        base_url,
        timeout=timeout,
    )

    if not post:
        return []

    post_html = str(post.get("html") or "")

    if media_type == "series":
        post_html = _extract_season_html(
            post_html,
            season,
        )

    post_links = _extract_post_links(
        post_html,
        base_url,
    )[:max_post_links]

    if not post_links:
        return []

    candidates = []

    for item in post_links:
        resolved = _resolve_outer_page(
            item["href"],
            referer=base_url.rstrip("/") + "/",
            quality=item.get("quality") or "HD",
            season=season,
            episode=episode,
            domains=domains,
            base_url=base_url,
            timeout=timeout,
        )

        candidates.extend(resolved)

        if len(candidates) >= max_streams:
            break

    seen = set()
    streams = []

    for candidate in candidates:
        url = str(candidate.get("url") or "").strip()

        if not url or url in seen:
            continue

        if validate_streams and not _validate_direct_media(
            url,
            str(candidate.get("referer") or base_url + "/"),
            timeout=min(timeout, 2.5),
        ):
            continue

        seen.add(url)

        stream = _make_stream(
            candidate,
            title=title,
            year=year,
            media_type=media_type,
            season=season,
            episode=episode,
        )

        if ui_smoke_test:
            stream["title"] = (
                "[MegaSource Test]\n"
                + str(stream.get("title") or "")
            )

        streams.append(stream)

        if len(streams) >= max_streams:
            break

    streams.sort(
        key=lambda item: (
            int(item.get("_res_weight") or 0),
            float(item.get("_size_weight") or 0),
        ),
        reverse=True,
    )

    for stream in streams:
        stream.pop("_res_weight", None)
        stream.pop("_size_weight", None)

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
