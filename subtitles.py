"""subtitles.py — StreamVault subtitle discovery, indexing, and caching.

Architecture
------------
The core problem: subtitle SRT/VTT files in Telegram channels can be uploaded
thousands of messages away from the matching video — before OR after. A naive
sliding-window scan (±N messages) will always miss them.

Solution: a PERSISTENT subtitle index backed by SQLite.

  subtitle_index.db
  └── table: subtitle_files
      ├── msg_id      INTEGER PRIMARY KEY  — Telegram message ID
      ├── filename    TEXT                 — e.g. "Dragon.S02E04.srt"
      ├── season      INTEGER              — extracted season number (or NULL)
      ├── episode     INTEGER              — extracted episode number (or NULL)
      ├── ext         TEXT                 — srt / vtt / ass / sub / zip
      └── indexed_at  REAL                 — unix timestamp of index run

On every app launch / channel refresh this index is updated incrementally
(only new messages since last scan). When a subtitle is needed for a video,
we query the index for the best-scoring candidate across the ENTIRE channel
history, then download + cache it on demand.
"""

import os
import asyncio
import re
import sqlite3
import time
import zipfile
import io
import difflib
import aiohttp
import config as _cfg
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename

# ── Paths ──────────────────────────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
SUB_DIR = os.path.join(_here, ".cache", "subtitles")
os.makedirs(SUB_DIR, exist_ok=True)

_INDEX_DB = os.path.join(_here, ".cache", "subtitle_index.db")
_SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".sub", ".zip"}

# ── Noise words to strip during fuzzy matching ─────────────────────────────────
_NOISE_WORDS = {
    "1080p", "720p", "480p", "360p", "2160p", "4k", "uhd", "hdr", "hdr10",
    "bluray", "bdrip", "brrip", "webrip", "webdl", "web-dl", "hdrip", "hdtv",
    "dvdrip", "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "10bit",
    "aac", "ac3", "eac3", "dts", "flac", "mp3", "opus", "ddp5", "5.1", "7.1",
    "repack", "proper", "unrated", "extended", "remastered", "theatrical",
    "yify", "yts", "psa", "eztv", "rarbg", "galaxytv", "tgx", "ettv",
    "eng", "english", "sdh", "sub", "subs", "subtitle", "subtitles",
    "srt", "vtt", "ass", "mp4", "mkv", "avi", "webm", "mov",
}


# ── Subtitle Index DB ──────────────────────────────────────────────────────────

def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_INDEX_DB, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subtitle_files (
            msg_id      INTEGER PRIMARY KEY,
            filename    TEXT NOT NULL,
            season      INTEGER,
            episode     INTEGER,
            ext         TEXT,
            indexed_at  REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def _get_last_indexed_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM index_state WHERE key='last_id'").fetchone()
    return int(row[0]) if row else 0


def _set_last_indexed_id(conn: sqlite3.Connection, msg_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO index_state(key,value) VALUES('last_id',?)",
        (str(msg_id),),
    )
    conn.commit()


# ── Season / Episode extraction ────────────────────────────────────────────────

def extract_season_episode(text: str) -> tuple[int | None, int | None]:
    """Extract Season and Episode numbers from a string.
    Returns (season, episode) or (None, None).
    Handles: S02E04, 2x04, Season 2 Episode 4, Ep 4, Part 4, etc.
    """
    if not text:
        return None, None
    # Normalize delimiters so word boundaries work reliably
    norm = re.sub(r"[._\-+\[\]()]", " ", text)

    # S01E04, S1 E4, S01  E04
    m = re.search(r"(?i)\bS(\d{1,2})\s*E(\d{1,3})\b", norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 1x04, 01x04
    m = re.search(r"(?i)\b(\d{1,2})\s*x\s*(\d{1,3})\b", norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Season 1 Episode 4 / Season 01 Ep 4
    m = re.search(r"(?i)\bSeason\s*(\d{1,2})\s*.*?Ep(?:isode)?\s*(\d{1,3})\b", norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Standalone: Episode 4 / Ep. 4 / E04
    m = re.search(r"(?i)\b(?:Ep(?:isode)?|E)\s*(\d{1,3})\b", norm)
    if m:
        return None, int(m.group(1))

    # Part 4 / Pt 4
    m = re.search(r"(?i)\b(?:Part|Pt)\s*(\d{1,3})\b", norm)
    if m:
        return None, int(m.group(1))

    return None, None


# ── Text tokenization ──────────────────────────────────────────────────────────

def tokenize_clean(text: str) -> set[str]:
    """Break text into significant keyword tokens, stripping release noise."""
    if not text:
        return set()
    cleaned = re.sub(r"[._\-\[\](){}+,;:!?'\"/\\]", " ", text.lower())
    return {w for w in cleaned.split() if len(w) >= 2 and w not in _NOISE_WORDS}


# ── Fuzzy match scoring ────────────────────────────────────────────────────────

def calculate_match_score(
    video_title: str, video_filename: str, sub_filename: str, distance: int = 0
) -> float:
    """Score a subtitle candidate against a video (0 = worst, higher = better).

    Returns -1000 if there is a definitive mismatch (wrong episode number),
    signalling the caller to reject this candidate.
    """
    v_combined = f"{video_title} {video_filename}".lower()
    s_clean = sub_filename.lower()

    # 1. Season / Episode agreement
    v_s, v_e = extract_season_episode(v_combined)
    s_s, s_e = extract_season_episode(s_clean)
    score = 0.0

    if v_e is not None:
        if s_e is not None:
            if v_e != s_e:
                return -1000.0          # Wrong episode — hard reject
            score += 55.0               # Episode match
            if v_s is not None and s_s is not None:
                if v_s != s_s:
                    return -1000.0      # Wrong season — hard reject
                score += 35.0           # Both season + episode match
        else:
            score -= 10.0               # No ep tag in sub name
    elif s_e is not None:
        score -= 20.0                   # Sub has episode tag, video doesn't

    # 2. Keyword / token overlap (Jaccard + subset ratio)
    v_tok = tokenize_clean(v_combined)
    s_tok = tokenize_clean(s_clean)
    if v_tok and s_tok:
        overlap = v_tok & s_tok
        jaccard = len(overlap) / max(len(v_tok | s_tok), 1)
        score += jaccard * 40.0
        score += (len(overlap) / max(len(v_tok), 1)) * 25.0

    # 3. String similarity (SequenceMatcher)
    sim = difflib.SequenceMatcher(
        None,
        " ".join(sorted(v_tok)),
        " ".join(sorted(s_tok)),
    ).ratio()
    score += sim * 20.0

    # 4. Direct substring bonus
    norm_t = re.sub(r"[^\w\s]", "", video_title.lower()).strip()
    norm_s = re.sub(r"[^\w\s]", "", s_clean).strip()
    if norm_t and (norm_t in norm_s or norm_s in norm_t):
        score += 25.0

    # 5. Proximity tie-breaker (max +10 for adjacent messages)
    score += max(0.0, 10.0 - distance * 0.001)   # ← tiny weight; distance can be huge

    return score


# ── Background subtitle index builder ─────────────────────────────────────────

async def build_subtitle_index(incremental: bool = True) -> int:
    """Scan the Telegram channel for subtitle files and persist them in the local DB.

    This is the key function that eliminates the "±N window" problem.
    It runs a full or incremental pass over the channel — just like _fetch_all()
    does for videos — and stores every subtitle it finds in subtitle_index.db.

    Args:
        incremental: If True, only scan messages newer than the last scan.
                     If False, do a full channel rescan (e.g. after /refresh).

    Returns:
        Number of new subtitle entries added this run.
    """
    from streaming import _get_entity

    client = _cfg.client
    if not client:
        return 0

    ent = await _get_entity()
    if not ent:
        return 0

    conn = _db_connect()
    last_id = _get_last_indexed_id(conn) if incremental else 0

    new_count = 0
    max_id_seen = last_id
    batch = 0

    print(f"[subtitle_index] Scanning from msg_id={last_id} (incremental={incremental})")

    async for msg in client.iter_messages(ent, reverse=True, min_id=last_id):
        if msg.id > max_id_seen:
            max_id_seen = msg.id

        batch += 1
        if batch % 500 == 0:
            await asyncio.sleep(0)      # Yield to aiohttp every 500 msgs

        if not msg.media or not isinstance(msg.media, MessageMediaDocument):
            continue

        doc_name = ""
        for attr in msg.media.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                doc_name = attr.file_name
                break

        if not doc_name:
            continue

        ext = os.path.splitext(doc_name)[1].lower()
        if ext not in _SUBTITLE_EXTS:
            continue

        season, episode = extract_season_episode(doc_name)

        # Upsert into the index
        conn.execute(
            """INSERT OR REPLACE INTO subtitle_files
               (msg_id, filename, season, episode, ext, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (msg.id, doc_name, season, episode, ext, time.time()),
        )
        new_count += 1

    if new_count:
        conn.commit()
    
    _set_last_indexed_id(conn, max_id_seen)
    conn.close()

    print(f"[subtitle_index] Done. {new_count} subtitle(s) indexed. max_id={max_id_seen}")
    return new_count


def query_subtitle_index(
    video_title: str,
    video_filename: str,
    season: int | None = None,
    episode: int | None = None,
) -> list[tuple[int, str, float]]:
    """Query the local subtitle index for candidates matching a video.

    Uses season/episode pre-filtering when available (O(log n) index scan),
    then fuzzy-scores the remaining candidates and returns them sorted by score.

    Returns:
        List of (msg_id, filename, score) tuples, best match first.
    """
    conn = _db_connect()
    
    try:
        if season is not None and episode is not None:
            # Fast path: filter by season + episode first
            rows = conn.execute(
                "SELECT msg_id, filename FROM subtitle_files WHERE season=? AND episode=?",
                (season, episode),
            ).fetchall()
            if not rows:
                # Retry with episode only (some subs omit season number)
                rows = conn.execute(
                    "SELECT msg_id, filename FROM subtitle_files WHERE episode=? AND season IS NULL",
                    (episode,),
                ).fetchall()
        elif episode is not None:
            rows = conn.execute(
                "SELECT msg_id, filename FROM subtitle_files WHERE episode=?",
                (episode,),
            ).fetchall()
        else:
            # No episode info — fetch all and rely purely on fuzzy scoring
            rows = conn.execute(
                "SELECT msg_id, filename FROM subtitle_files"
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    scored: list[tuple[int, str, float]] = []
    for msg_id, filename in rows:
        sc = calculate_match_score(video_title, video_filename, filename)
        if sc > 15.0:
            scored.append((msg_id, filename, sc))

    scored.sort(key=lambda x: x[2], reverse=True)
    return scored


# ── Yify online fallback ───────────────────────────────────────────────────────

async def fetch_from_yify(msg_id: int, imdb_id: str) -> str | None:
    """Query yifysubtitles.ch for English subtitle and cache it."""
    if not imdb_id or not imdb_id.startswith("tt"):
        return None

    url = f"https://yifysubtitles.ch/movie-imdb/{imdb_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

                matches = re.findall(r'href="(/subtitles/[^"]+-english-yify-[^"]+)"', html)
                if not matches:
                    matches = re.findall(
                        r'href="(/subtitles/[^"]+)"[^>]*>.*?English',
                        html, re.DOTALL | re.IGNORECASE,
                    )
                if not matches:
                    return None

                sub_page_url = f"https://yifysubtitles.ch{matches[0]}"
                async with sess.get(sub_page_url, headers=headers, timeout=10) as sub_resp:
                    if sub_resp.status != 200:
                        return None
                    sub_html = await sub_resp.text()

                    zip_matches = re.findall(r'href="(/subtitle/[^"]+\.zip)"', sub_html)
                    if not zip_matches:
                        slug = matches[0].replace("/subtitles/", "")
                        zip_url = f"https://yifysubtitles.ch/subtitle/{slug}.zip"
                    else:
                        zip_url = f"https://yifysubtitles.ch{zip_matches[0]}"

                    async with sess.get(zip_url, headers=headers, timeout=15) as zip_resp:
                        if zip_resp.status != 200:
                            return None
                        zip_data = await zip_resp.read()
                        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                            for name in z.namelist():
                                if name.lower().endswith(".srt"):
                                    srt_content = z.read(name)
                                    cache_path = os.path.join(SUB_DIR, f"{msg_id}_en.srt")
                                    with open(cache_path, "wb") as f:
                                        f.write(srt_content)
                                    print(f"[subtitles] Yify: saved {name}")
                                    return cache_path
    except Exception as e:
        print(f"[subtitles] Yify error for imdb_id={imdb_id}: {e}")

    return None


# ── Download & cache a subtitle message ───────────────────────────────────────

async def _download_subtitle_msg(
    client, entity, msg_id: int, sub_msg_id: int, filename: str
) -> str | None:
    """Download a subtitle Telegram message and save it to the local cache."""
    cache_path = os.path.join(SUB_DIR, f"{msg_id}_en.srt")
    temp_path = cache_path + ".tmp"

    try:
        msgs = await client.get_messages(entity, ids=[sub_msg_id])
        if not msgs or not msgs[0]:
            return None
        sub_msg = msgs[0]

        await client.download_media(sub_msg, file=temp_path)

        ext = os.path.splitext(filename)[1].lower()

        if ext == ".zip":
            try:
                with zipfile.ZipFile(temp_path, "r") as z:
                    srt_files = [
                        n for n in z.namelist()
                        if os.path.splitext(n)[1].lower() in {".srt", ".vtt", ".ass"}
                    ]
                    if srt_files:
                        content = z.read(srt_files[0])
                        with open(cache_path, "wb") as f:
                            f.write(content)
                        os.remove(temp_path)
                        return cache_path
            except Exception as ze:
                print(f"[subtitles] Zip extract failed: {ze}")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return None

        elif ext == ".vtt":
            with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            content = re.sub(r"^WEBVTT\s*\n", "", content)
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.remove(temp_path)
            return cache_path

        else:  # .srt, .ass, .sub
            if os.path.exists(cache_path):
                os.remove(cache_path)
            os.rename(temp_path, cache_path)
            return cache_path

    except Exception as e:
        print(f"[subtitles] Download error sub_msg_id={sub_msg_id}: {e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return None


# ── Main entry point ───────────────────────────────────────────────────────────

async def fetch_english_subtitle(msg_id: int, title: str, filename: str) -> str | None:
    """Find, download and cache the best subtitle for a given video.

    Search order:
    1. Local file cache (instant).
    2. Full-channel subtitle index (covers entire channel history, not just ±N).
    3. Yify online fallback (movies with IMDb ID).
    """
    # 1. Local cache hit
    cache_path = os.path.join(SUB_DIR, f"{msg_id}_en.srt")
    if os.path.exists(cache_path):
        return cache_path

    try:
        from streaming import _get_entity
        client = _cfg.client
        if not client:
            return None
        entity = await _get_entity()
        if not entity:
            return None

        # 2. Query the full-channel subtitle index
        v_s, v_e = extract_season_episode(f"{title} {filename}")
        candidates = query_subtitle_index(title, filename, season=v_s, episode=v_e)

        if candidates:
            best_msg_id, best_filename, best_score = candidates[0]
            print(
                f"[subtitles] Index match for '{title}': "
                f"'{best_filename}' score={best_score:.1f} (sub_msg={best_msg_id})"
            )
            path = await _download_subtitle_msg(
                client, entity, msg_id, best_msg_id, best_filename
            )
            if path:
                return path

        # No index hit — run a one-time incremental index build and retry
        print(f"[subtitles] No index candidates for '{title}', triggering index scan...")
        added = await build_subtitle_index(incremental=True)
        if added > 0:
            candidates = query_subtitle_index(title, filename, season=v_s, episode=v_e)
            if candidates:
                best_msg_id, best_filename, best_score = candidates[0]
                path = await _download_subtitle_msg(
                    client, entity, msg_id, best_msg_id, best_filename
                )
                if path:
                    return path

    except Exception as e:
        print(f"[subtitles] Index lookup error for msg_id={msg_id}: {e}")

    # 3. Yify online fallback (movies)
    try:
        import cache as _cache
        video = _cache._cache_meta.get(msg_id, {})
        pm = _cache._poster_mem.get(video.get("album", ""), {})
        imdb_id = pm.get("meta", {}).get("imdb_id")
        if imdb_id:
            path = await fetch_from_yify(msg_id, imdb_id)
            if path:
                return path
    except Exception as e:
        print(f"[subtitles] Yify fallback error: {e}")

    return None
