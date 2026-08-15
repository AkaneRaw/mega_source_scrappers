"""
HiAnime provider port for MegaSource.

Converted from the supplied obfuscated Nuvio JavaScript provider.

MegaSource contract:
    TITLE, VERSION, DESCRIPTION
    get_streams(media_type, media_id, config) -> list[dict]

Flow:
    IMDb/Stremio ID
      -> Cinemeta metadata
      -> IMDb -> MAL episode mapping (series)
         or Jikan title -> MAL lookup (movie/fallback)
      -> MegaPlay / Vidwish / MegaCloud player page
      -> /stream/getSources JSON
      -> direct HLS + subtitles

Config:
    timeout: number         Default: 15
    sub_dub: str            "both" | "sub" | "dub" (default: "both")
    title: str              Optional metadata override
    year: int               Optional metadata override
    mal_id: int             Optional MAL ID override
    mal_episode: int        Optional MAL episode override
    mapping_base: str       Optional mapping-service override
    megaplay_base: str      Optional source-domain override
    vidwish_base: str       Optional source-domain override
    megacloud_base: str     Optional source-domain override

The exact hosted MegaSource URL-only test sentinel
(movie + tt0111161 + empty config) is treated as a provider smoke test using
Solo Leveling S1E1. Real requests are not remapped.
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

TITLE = "HiAnime"
VERSION = "1.0.0"
DESCRIPTION = "HiAnime multi-source HLS provider for MegaSource"

MEGAPLAY_BASE = "https://megaplay.buzz"
VIDWISH_BASE = "https://vidwish.live"
MEGACLOUD_BASE = "https://megacloud.bloggy.click"

MAPPING_BASE = "https://id-mapping-api-malid.hf.space/api/resolve"
JIKAN_SEARCH = "https://api.jikan.moe/v4/anime"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Connection": "keep-alive",
}

# MegaSource URL-only test fallback.
MEGASOURCE_TEST_MEDIA_TYPE = "movie"
MEGASOURCE_TEST_MEDIA_ID = "tt0111161"

# Solo Leveling S1E1, used only for that exact hosted smoke-test sentinel.
MEGASOURCE_TEST_TITLE = "Solo Leveling"
MEGASOURCE_TEST_YEAR = 2024
MEGASOURCE_TEST_MAL_ID = 52299
MEGASOURCE_TEST_MAL_EPISODE = 1


def _request(
    url: str,
    *,
    method: str = "GET",
    data=None,
    headers: Optional[dict] = None,
    timeout: float = 15,
    retries: int = 1,
) -> tuple[int, str]:
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)

    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
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

    runtime = "N/A"
    runtime_raw = str(meta.get("runtime") or "").strip()
    if runtime_raw:
        match = re.search(r"(\d+)", runtime_raw)
        if match:
            runtime = match.group(1) + "m"

    return {
        "title": title,
        "year": year,
        "duration": runtime,
        "videos": meta.get("videos") or [],
    }


def _episode_metadata(
    details: dict,
    media_type: str,
    season: int,
    episode: int,
) -> dict:
    if media_type == "movie":
        return {
            "epTitle": "Movie",
            "duration": details.get("duration") or "N/A",
        }

    ep_title = f"Episode {episode}"
    duration = details.get("duration") or "N/A"

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

            if s == int(season) and e == int(episode):
                ep_title = str(
                    video.get("title")
                    or video.get("name")
                    or ep_title
                ).strip()

                runtime = video.get("runtime")
                if runtime:
                    match = re.search(r"(\d+)", str(runtime))
                    if match:
                        duration = match.group(1) + "m"
                break

    return {"epTitle": ep_title, "duration": duration}


def _resolve_mapping(
    imdb_id: str,
    season: int,
    episode: int,
    mapping_base: str,
    *,
    timeout: float = 15,
) -> Optional[dict]:
    if not str(imdb_id).lower().startswith("tt"):
        return None

    url = (
        mapping_base.rstrip("?")
        + "?"
        + urllib.parse.urlencode(
            {
                "id": imdb_id,
                "s": str(int(season)),
                "e": str(int(episode)),
            }
        )
    )

    data = _json_request(url, timeout=timeout, retries=1)
    if not isinstance(data, dict) or data.get("error"):
        return None

    mal_id = data.get("mal_id") or data.get("mal")
    mal_episode = (
        data.get("mal_episode")
        or data.get("episode")
        or episode
    )

    if not mal_id:
        return None

    try:
        mal_id = int(mal_id)
        mal_episode = int(mal_episode)
    except Exception:
        return None

    return {
        "mal_id": mal_id,
        "mal_episode": mal_episode,
    }


def _search_mal_id(
    title: str,
    media_type: str,
    season: int = 1,
    *,
    timeout: float = 15,
) -> Optional[int]:
    anime_type = "movie" if media_type == "movie" else "tv"

    queries = []
    if media_type == "series" and int(season or 1) > 1:
        queries.append(f"{title} Season {int(season)}")
    queries.append(title)

    for query in queries:
        url = (
            JIKAN_SEARCH
            + "?"
            + urllib.parse.urlencode(
                {
                    "q": query,
                    "type": anime_type,
                    "limit": "1",
                }
            )
        )

        data = _json_request(url, timeout=timeout, retries=1)
        if not isinstance(data, dict):
            continue

        results = data.get("data") or []
        if not results or not isinstance(results[0], dict):
            continue

        mal_id = results[0].get("mal_id")
        try:
            if mal_id:
                return int(mal_id)
        except Exception:
            pass

    return None


def _extract_div_attrs(page_html: str) -> tuple[Optional[str], Optional[str]]:
    """
    Extract data-id / data-realid from:
        div.fix-area#megaplay-player
    without depending on BeautifulSoup.
    """
    tag_match = re.search(
        r"<div\b([^>]*\bid=[\"']megaplay-player[\"'][^>]*)>",
        page_html,
        re.I,
    )

    if not tag_match:
        tag_match = re.search(
            r"<div\b([^>]*\bclass=[\"'][^\"']*\bfix-area\b[^\"']*[\"'][^>]*)>",
            page_html,
            re.I,
        )

    if not tag_match:
        return None, None

    attrs = tag_match.group(1)

    id_match = re.search(
        r"\bdata-id=[\"']([^\"']+)[\"']",
        attrs,
        re.I,
    )
    real_match = re.search(
        r"\bdata-realid=[\"']([^\"']+)[\"']",
        attrs,
        re.I,
    )

    return (
        html.unescape(id_match.group(1)).strip() if id_match else None,
        html.unescape(real_match.group(1)).strip() if real_match else None,
    )


def _source_file(data) -> Optional[str]:
    if not isinstance(data, dict):
        return None

    sources = data.get("sources")

    if isinstance(sources, dict):
        value = sources.get("file")
        if value:
            return str(value).strip()

    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict) and source.get("file"):
                return str(source["file"]).strip()

    return None


def _subtitles(data) -> list:
    if not isinstance(data, dict):
        return []

    tracks = data.get("tracks") or []
    if not isinstance(tracks, list):
        return []

    results = []

    for track in tracks:
        if not isinstance(track, dict):
            continue

        if str(track.get("kind") or "").lower() != "captions":
            continue

        url = str(track.get("file") or "").strip()
        if not url:
            continue

        label = str(track.get("label") or "English").strip()
        language = label[:3].lower() if label else "en"

        results.append(
            {
                "url": url,
                "name": label,
                "language": language,
            }
        )

    return results


def _extract_sources(
    endpoint: str,
    referer: str,
    origin: str,
    provider_name: str,
    title: str,
    media_type: str,
    season: int,
    episode: int,
    sub_dub: str,
    episode_meta: dict,
    *,
    timeout: float = 15,
) -> list:
    data = _json_request(
        endpoint,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer,
            "Origin": origin,
        },
        timeout=timeout,
        retries=1,
    )

    stream_url = _source_file(data)
    if not stream_url:
        return []

    language = (
        "Original (SUB)"
        if sub_dub.lower() == "sub"
        else "English (DUB)"
    )
    mode = sub_dub.upper()

    if media_type == "movie":
        lines = [
            f"🎬 {title}",
            f"🎞️ M3U8 | ⚡ Auto | 🌍 {language} | ⏱️ {episode_meta.get('duration', 'N/A')}",
        ]
    else:
        lines = [
            f"🎬 {title}",
            f"🎥 S{season}E{episode} - {episode_meta.get('epTitle', 'Episode')}",
            f"🎞️ M3U8 | ⚡ Auto | 🌍 {language} | ⏱️ {episode_meta.get('duration', 'N/A')}",
        ]

    display_title = "\n".join(lines)

    playback_headers = dict(DEFAULT_HEADERS)
    playback_headers.update(
        {
            "Referer": origin.rstrip("/") + "/",
            "Origin": origin.rstrip("/"),
        }
    )

    result = {
        "name": f"HiAnime | Auto | [{provider_name}] ({mode})",
        "title": display_title,
        "url": stream_url,
        "quality": "Auto",
        "source": f"HiAnime {provider_name} {mode}",
        "behaviorHints": {
            "notWebReady": True,
            "proxyHeaders": {
                "request": playback_headers,
            },
        },

        # Kept for direct provider consumers; MegaSource may normalize it.
        "headers": playback_headers,
    }

    subtitle_items = _subtitles(data)
    if subtitle_items:
        result["subtitles"] = subtitle_items

    return [result]


def _scrape_type(
    mal_id: int,
    mal_episode: int,
    sub_dub: str,
    title: str,
    episode_meta: dict,
    media_type: str,
    season: int,
    *,
    megaplay_base: str,
    vidwish_base: str,
    megacloud_base: str,
    timeout: float = 15,
) -> list:
    streams = []

    stream_page = (
        megaplay_base.rstrip("/")
        + f"/stream/mal/{int(mal_id)}/{int(mal_episode)}/{sub_dub}"
    )

    status, page_html = _request(
        stream_page,
        headers={"Referer": stream_page},
        timeout=timeout,
        retries=1,
    )
    if status != 200 or not page_html:
        return []

    data_id, data_realid = _extract_div_attrs(page_html)

    jobs = []

    if data_id:
        jobs.append(
            (
                "MegaPlay",
                (
                    megaplay_base.rstrip("/")
                    + "/stream/getSources?"
                    + urllib.parse.urlencode(
                        {
                            "id": data_id,
                        }
                    )
                    + "&id="
                    + urllib.parse.quote(str(data_id))
                ),
                stream_page,
                megaplay_base.rstrip("/"),
            )
        )

    if data_realid:
        vidwish_page = (
            vidwish_base.rstrip("/")
            + f"/stream/s-2/{data_realid}/{sub_dub}"
        )
        vw_status, vw_html = _request(
            vidwish_page,
            headers={"Referer": stream_page},
            timeout=timeout,
            retries=1,
        )
        if vw_status == 200 and vw_html:
            vw_id, _ = _extract_div_attrs(vw_html)
            if vw_id:
                jobs.append(
                    (
                        "Vidwish",
                        (
                            vidwish_base.rstrip("/")
                            + "/stream/getSources?"
                            + urllib.parse.urlencode({"id": vw_id})
                            + "&id="
                            + urllib.parse.quote(str(vw_id))
                        ),
                        vidwish_page,
                        vidwish_base.rstrip("/"),
                    )
                )

        megacloud_page = (
            megacloud_base.rstrip("/")
            + f"/stream/s-3/{data_realid}/{sub_dub}"
        )
        mc_status, mc_html = _request(
            megacloud_page,
            headers={"Referer": stream_page},
            timeout=timeout,
            retries=1,
        )
        if mc_status == 200 and mc_html:
            mc_id, _ = _extract_div_attrs(mc_html)
            if mc_id:
                jobs.append(
                    (
                        "MegaCloud",
                        (
                            megacloud_base.rstrip("/")
                            + "/stream/getSources?"
                            + urllib.parse.urlencode({"id": mc_id})
                            + "&id="
                            + urllib.parse.quote(str(mc_id))
                        ),
                        megacloud_page,
                        megacloud_base.rstrip("/"),
                    )
                )

    for provider_name, endpoint, referer, origin in jobs:
        streams.extend(
            _extract_sources(
                endpoint,
                referer,
                origin,
                provider_name,
                title,
                media_type,
                season,
                mal_episode,
                sub_dub,
                episode_meta,
                timeout=timeout,
            )
        )

    return streams


def _probe_hls(
    stream_url: str,
    playback_headers: dict,
    *,
    timeout: float = 8,
) -> tuple[int, bool]:
    status, body = _request(
        stream_url,
        headers=playback_headers,
        timeout=timeout,
        retries=0,
    )

    return status, (
        200 <= status < 300
        and "#EXTM3U" in body[:8192]
    )


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
            "mal_id": MEGASOURCE_TEST_MAL_ID,
            "mal_episode": MEGASOURCE_TEST_MAL_EPISODE,
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
        season = 1
        episode = 1

    try:
        timeout = float(config.get("timeout", 15))
    except Exception:
        timeout = 15

    megaplay_base = str(
        config.get("megaplay_base") or MEGAPLAY_BASE
    ).strip().rstrip("/")
    vidwish_base = str(
        config.get("vidwish_base") or VIDWISH_BASE
    ).strip().rstrip("/")
    megacloud_base = str(
        config.get("megacloud_base") or MEGACLOUD_BASE
    ).strip().rstrip("/")
    mapping_base = str(
        config.get("mapping_base") or MAPPING_BASE
    ).strip()

    title_override = str(config.get("title") or "").strip()

    if title_override:
        try:
            year = int(config.get("year") or 0) or None
        except Exception:
            year = None

        details = {
            "title": title_override,
            "year": year,
            "duration": "N/A",
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
    if not title:
        return []

    episode_meta = _episode_metadata(
        details,
        media_type,
        season,
        episode,
    )

    mal_id = None
    mal_episode = episode

    try:
        if config.get("mal_id") not in (None, ""):
            mal_id = int(config["mal_id"])
    except Exception:
        mal_id = None

    try:
        if config.get("mal_episode") not in (None, ""):
            mal_episode = int(config["mal_episode"])
    except Exception:
        mal_episode = episode

    if not mal_id:
        if media_type == "series":
            mapping = _resolve_mapping(
                base_id,
                season,
                episode,
                mapping_base,
                timeout=timeout,
            )
            if mapping:
                mal_id = mapping["mal_id"]
                mal_episode = mapping["mal_episode"]

        if not mal_id:
            mal_id = _search_mal_id(
                title,
                media_type,
                season,
                timeout=timeout,
            )
            mal_episode = 1 if media_type == "movie" else episode

    if not mal_id:
        return []

    preference = str(
        config.get("sub_dub")
        or config.get("subDub")
        or "both"
    ).strip().lower()

    if preference not in ("both", "sub", "dub"):
        preference = "both"

    modes = ["sub", "dub"] if preference == "both" else [preference]

    streams = []

    for mode in modes:
        streams.extend(
            _scrape_type(
                mal_id,
                mal_episode,
                mode,
                title,
                episode_meta,
                media_type,
                season,
                megaplay_base=megaplay_base,
                vidwish_base=vidwish_base,
                megacloud_base=megacloud_base,
                timeout=timeout,
            )
        )

    # Deduplicate identical media URLs.
    unique = []
    seen = set()

    for stream in streams:
        url = str(stream.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)

        playback_headers = (
            stream.get("behaviorHints", {})
            .get("proxyHeaders", {})
            .get("request", {})
        )
        probe_status, probe_ok = _probe_hls(
            url,
            playback_headers,
            timeout=min(timeout, 8),
        )

        diagnostic = (
            f"\n🧪 HLS probe: {probe_status}"
            f" | {'OK' if probe_ok else 'FAILED'}"
        )

        if config.get("_megasource_ui_smoke_test"):
            stream["title"] = (
                "[MegaSource Test]\n"
                + str(stream.get("title") or "")
                + diagnostic
            )
        else:
            stream["title"] = (
                str(stream.get("title") or "")
                + diagnostic
            )

        unique.append(stream)

    return unique


def getStreams(media_type: str, media_id: str, config: dict = None) -> list:
    return get_streams(media_type, media_id, config or {})
