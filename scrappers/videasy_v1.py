"""
VidEasy provider port for MegaSource.

Converted from the supplied JavaScript provider.

Important:
- This port keeps the public metadata and Wings API request flow.
- It intentionally does NOT reproduce the provider's custom encrypted-payload
  decryption routine.
- A server result is used only when the public API returns plaintext JSON with
  "sources" / "subtitles".

MegaSource contract:
    TITLE, VERSION, DESCRIPTION
    get_streams(media_type, media_id, config) -> list[dict]

Config:
    timeout: number         Default: 8
    tmdb_id: int            Optional direct TMDB ID override
    tmdb_api_key: str       Optional TMDB Find-by-ID fallback
    title: str              Optional title override
    year: int               Optional year override
    wings_api_base: str     Optional API base override
    max_servers: int        Default: 10
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

TITLE = "VidEasy"
VERSION = "1.0.0"
DESCRIPTION = "VidEasy public/plaintext Wings source provider for MegaSource"

WINGS_API_BASE = "https://api.speedracelight.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Origin": "https://www.vidking.net",
    "Referer": "https://www.vidking.net/",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

SERVERS = [
    ("Hydrogen", "cdn/sources-with-title"),
    ("Titanium", "tejo/sources-with-title"),
    ("Oxygen", "neon2/sources-with-title"),
    ("Lithium", "downloader2/sources-with-title"),
    ("Krypton", "ym/sources-with-title"),
    ("Carbon", "mb-flix/sources-with-title"),
    ("Aluminium", "lamovie/sources-with-title"),
    ("Nitrogen", "m4uhd/sources-with-title"),
    ("Neon", "superflix/sources-with-title"),
    ("Helium", "1movies/sources-with-title"),
]

SERVER_PROVIDER_NAMES = {
    "Hydrogen": "CDN",
    "Titanium": "Tejo",
    "Oxygen": "Neon2",
    "Lithium": "Downloader2",
    "Krypton": "YM",
    "Carbon": "MB-Flix",
    "Aluminium": "LaMovie",
    "Nitrogen": "M4UHD",
    "Neon": "SuperFlix",
    "Helium": "1Movies",
}

SERVER_ICONS = {
    "Hydrogen": "💧",
    "Titanium": "🛡️",
    "Oxygen": "💨",
    "Lithium": "🔋",
    "Krypton": "🦸",
    "Carbon": "💎",
    "Aluminium": "💿",
    "Nitrogen": "🌿",
    "Neon": "💡",
    "Helium": "🎈",
}

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"


def _request(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: float = 8,
    retries: int = 0,
) -> tuple[int, str]:
    req_headers = dict(REQUEST_HEADERS)
    if headers:
        req_headers.update(headers)

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=req_headers, method="GET")
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

        time.sleep(0.25 * (attempt + 1))

    return 0, ""


def _json_request(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: float = 8,
):
    status, body = _request(
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
    timeout: float = 8,
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

    duration = "45 min" if media_type == "series" else "90 min"
    runtime = str(meta.get("runtime") or "")
    runtime_match = re.search(r"(\d+)", runtime)
    if runtime_match:
        duration = runtime_match.group(1) + " min"

    return {
        "title": title,
        "year": year or "N/A",
        "duration": duration,
        "imdb_id": imdb_id,
    }


def _resolve_tmdb_wikidata(
    imdb_id: str,
    media_type: str,
    *,
    timeout: float = 8,
) -> Optional[int]:
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
            "User-Agent": "MegaSource-VidEasy/1.0",
        },
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return None

    try:
        value = data["results"]["bindings"][0]["tmdb"]["value"]
        return int(value)
    except Exception:
        return None


def _resolve_tmdb_api(
    imdb_id: str,
    media_type: str,
    api_key: str,
    *,
    timeout: float = 8,
) -> Optional[int]:
    if not api_key:
        return None

    url = (
        "https://api.themoviedb.org/3/find/"
        + urllib.parse.quote(imdb_id)
        + "?"
        + urllib.parse.urlencode(
            {
                "api_key": api_key,
                "external_source": "imdb_id",
            }
        )
    )

    data = _json_request(url, timeout=timeout)
    if not isinstance(data, dict):
        return None

    key = "tv_results" if media_type == "series" else "movie_results"
    results = data.get(key) or []

    try:
        return int(results[0]["id"])
    except Exception:
        return None


def _resolve_tmdb_id(
    base_id: str,
    media_type: str,
    config: dict,
    *,
    timeout: float = 8,
) -> Optional[int]:
    try:
        if config.get("tmdb_id") not in (None, ""):
            return int(config["tmdb_id"])
    except Exception:
        pass

    if str(base_id).isdigit():
        return int(base_id)

    tmdb_id = _resolve_tmdb_wikidata(
        base_id,
        media_type,
        timeout=timeout,
    )
    if tmdb_id:
        return tmdb_id

    api_key = str(config.get("tmdb_api_key") or "").strip()
    return _resolve_tmdb_api(
        base_id,
        media_type,
        api_key,
        timeout=timeout,
    )


def _get_lang_code(value: str) -> str:
    mapping = {
        "english": "en",
        "spanish": "es",
        "french": "fr",
        "german": "de",
        "italian": "it",
        "portuguese": "pt",
        "portuguese (br)": "pt-br",
        "arabic": "ar",
        "japanese": "ja",
        "korean": "ko",
        "tamil": "ta",
        "telugu": "te",
        "malayalam": "ml",
        "kannada": "kn",
        "hindi": "hi",
        "polish": "pl",
        "greek": "el",
        "croatian": "hr",
        "ukrainian": "uk",
        "lithuanian": "lt",
        "thai": "th",
        "estonian": "et",
        "czech": "cs",
        "dutch": "nl",
        "indonesian": "id",
        "sinhala": "si",
        "swedish": "sv",
        "romanian": "ro",
        "malay": "ms",
        "persian": "fa",
        "slovak": "sk",
        "bulgarian": "bg",
        "turkish": "tr",
        "danish": "da",
        "hebrew": "he",
        "serbian": "sr",
        "vietnamese": "vi",
        "hungarian": "hu",
        "icelandic": "is",
        "albanian": "sq",
        "bosnian": "bs",
        "slovenian": "sl",
        "bengali": "bn",
        "macedonian": "mk",
    }
    return mapping.get(str(value or "").lower().strip(), "en")


def _quality_label(value: str):
    text = str(value or "1080p")
    clean = re.sub(r"\s*server\s*2\s*$", "", text, flags=re.I).strip()
    lower = clean.lower()

    if "2160" in lower or "4k" in lower:
        return clean, "2160p", "🌟 4K"
    if "1080" in lower:
        return clean, "1080p", "🔥 1080p"
    if "720" in lower:
        return clean, "720p", "💎 720p"
    if lower == "auto":
        return clean, "auto", "⚡ Auto"

    return clean, clean.lower(), "⚡ " + clean


def _format_plaintext_response(
    payload: dict,
    server_name: str,
    media: dict,
    season: Optional[int],
    episode: Optional[int],
) -> list:
    if not isinstance(payload, dict):
        return []

    sources = payload.get("sources") or []
    if not isinstance(sources, list):
        return []

    subtitle_headers = {
        "Referer": "https://www.vidking.net/",
        "Origin": "https://www.vidking.net",
        "User-Agent": USER_AGENT,
    }

    subtitles = []
    for sub in payload.get("subtitles") or []:
        if not isinstance(sub, dict):
            continue
        url = str(sub.get("url") or "").strip()
        if not url:
            continue

        lang_name = str(
            sub.get("language")
            or sub.get("lang")
            or "English"
        ).strip()

        subtitles.append(
            {
                "url": url,
                "language": _get_lang_code(lang_name),
                "name": str(sub.get("label") or sub.get("lang") or lang_name),
                "headers": subtitle_headers,
            }
        )

    streams = []
    provider_name = SERVER_PROVIDER_NAMES.get(server_name, server_name)
    icon = SERVER_ICONS.get(server_name, "🎬")

    for source in sources:
        if not isinstance(source, dict):
            continue

        url = str(source.get("url") or "").strip()
        if not url:
            continue

        raw_quality = source.get("quality") or "1080p"
        quality_name, quality, quality_display = _quality_label(raw_quality)

        lower_url = url.lower()
        fmt = "M3U8" if ".m3u8" in lower_url else "MP4" if ".mp4" in lower_url else "Direct"

        label_text = str(source.get("title") or "").lower()
        if "bengali" in label_text or "bangla" in label_text:
            audio = "🇧🇩 Bengali"
            audio_name = "Bengali"
        elif server_name == "Aluminium":
            audio = "🌐 Dual-Audio"
            audio_name = "Dual-Audio"
        else:
            audio = "🌍 Original Audio"
            audio_name = "Original Audio"

        title_text = str(media.get("title") or "Unknown")
        if media.get("mediaType") == "tv":
            title_text += f" S{int(season or 1)}E{int(episode or 1)}"

        display = "\n".join(
            [
                f"🎬 {title_text} ({media.get('year', 'N/A')})",
                f"{quality_display} | {audio} | 🎧 AAC",
                f"🎞️ {fmt} | ⏱️ {media.get('duration', 'N/A')}",
                f"{icon} {server_name} | 🔗 Provider: {provider_name}",
            ]
        )

        stream = {
            "name": f"VidEasy | {quality_name} | {audio_name}",
            "title": display,
            "url": url,
            "quality": quality,
            "source": f"VidEasy {server_name}",
            "behaviorHints": {
                "notWebReady": True,
                "proxyHeaders": {
                    "request": subtitle_headers,
                },
            },
            "headers": subtitle_headers,
        }

        if subtitles:
            stream["subtitles"] = subtitles

        streams.append(stream)

    return streams


def _fetch_plaintext_server(
    server_name: str,
    path: str,
    *,
    media_type: str,
    tmdb_id: int,
    media: dict,
    season: Optional[int],
    episode: Optional[int],
    seed: str,
    wings_api_base: str,
    timeout: float,
) -> list:
    params = {
        "title": str(media.get("title") or ""),
        "mediaType": "tv" if media_type == "series" else "movie",
        "year": str(media.get("year") or ""),
        "episodeId": str(int(episode or 1)),
        "seasonId": str(int(season or 1)),
        "tmdbId": str(tmdb_id),
        "imdbId": str(media.get("imdbId") or ""),
        "enc": "2",
        "seed": seed,
    }

    url = (
        wings_api_base.rstrip("/")
        + "/"
        + path.lstrip("/")
        + "?"
        + urllib.parse.urlencode(params)
    )

    status, body = _request(
        url,
        headers=REQUEST_HEADERS,
        timeout=timeout,
        retries=0,
    )
    if not (200 <= status < 300) or not body:
        return []

    # Safe/public path only: use response if it is already plaintext JSON.
    try:
        payload = json.loads(body)
    except Exception:
        return []

    return _format_plaintext_response(
        payload,
        server_name,
        media,
        season,
        episode,
    )


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
        season = 1
        episode = 1

    try:
        timeout = float(config.get("timeout", 8))
    except Exception:
        timeout = 8

    try:
        max_servers = max(1, min(10, int(config.get("max_servers", 10))))
    except Exception:
        max_servers = 10

    tmdb_id = _resolve_tmdb_id(
        base_id,
        media_type,
        config,
        timeout=min(timeout, 8),
    )
    if not tmdb_id:
        return []

    title_override = str(config.get("title") or "").strip()

    if title_override:
        try:
            year = int(config.get("year") or 0) or "N/A"
        except Exception:
            year = "N/A"

        media = {
            "title": title_override,
            "year": year,
            "duration": "45 min" if media_type == "series" else "90 min",
            "imdbId": base_id if str(base_id).lower().startswith("tt") else "",
            "mediaType": "tv" if media_type == "series" else "movie",
        }
    else:
        details = _cinemeta_details(
            base_id,
            media_type,
            timeout=min(timeout, 8),
        )
        if not details:
            return []

        media = {
            "title": details["title"],
            "year": details["year"],
            "duration": details["duration"],
            "imdbId": details["imdb_id"],
            "mediaType": "tv" if media_type == "series" else "movie",
        }

    wings_api_base = str(
        config.get("wings_api_base")
        or WINGS_API_BASE
    ).strip().rstrip("/")

    seed_url = (
        wings_api_base
        + "/seed?"
        + urllib.parse.urlencode({"mediaId": str(tmdb_id)})
    )

    seed_data = _json_request(
        seed_url,
        headers=REQUEST_HEADERS,
        timeout=min(timeout, 8),
    )
    if not isinstance(seed_data, dict):
        return []

    seed = str(seed_data.get("seed") or "").strip()
    if not seed:
        return []

    streams = []

    for server_name, path in SERVERS[:max_servers]:
        streams.extend(
            _fetch_plaintext_server(
                server_name,
                path,
                media_type=media_type,
                tmdb_id=tmdb_id,
                media=media,
                season=season,
                episode=episode,
                seed=seed,
                wings_api_base=wings_api_base,
                timeout=timeout,
            )
        )

    # Deduplicate URLs.
    result = []
    seen = set()

    for stream in streams:
        url = str(stream.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(stream)

    # 4K first, then server order.
    def rank(item):
        quality = str(item.get("quality") or "").lower()
        return 0 if quality in ("2160p", "4k") else 1

    result.sort(key=rank)
    return result


def getStreams(
    media_type: str,
    media_id: str,
    config: dict = None,
) -> list:
    return get_streams(media_type, media_id, config or {})
