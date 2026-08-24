import os
import asyncio
import re
import zipfile
import io
import difflib
import aiohttp
import config as _cfg
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename

# Subtitles cache directory
_here = os.path.dirname(os.path.abspath(__file__))
SUB_DIR = os.path.join(_here, ".cache", "subtitles")
os.makedirs(SUB_DIR, exist_ok=True)

# Common video/subtitle release noise words to filter out during fuzzy matching
_NOISE_WORDS = {
    "1080p", "720p", "480p", "360p", "2160p", "4k", "uhd", "hdr", "hdr10",
    "bluray", "bdrip", "brrip", "webrip", "webdl", "web-dl", "hdrip", "hdtv",
    "dvdrip", "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "10bit",
    "aac", "ac3", "eac3", "dts", "flac", "mp3", "opus", "ddp5", "5.1", "7.1",
    "repack", "proper", "unrated", "extended", "remastered", "theatrical",
    "yify", "yts", "psa", "eztv", "rarbg", "galaxytv", "tgx", "ettv",
    "eng", "english", "sdh", "sub", "subs", "subtitle", "subtitles",
    "srt", "vtt", "ass", "sub", "mp4", "mkv", "avi", "webm", "mov"
}


def extract_season_episode(text: str) -> tuple[int | None, int | None]:
    """Extract Season and Episode numbers from text (e.g. S02E04, 2x04, Episode 4, Ep 4).
    Returns (season, episode) or (None, None).
    """
    if not text:
        return None, None

    # Normalize delimiters like _, ., - into spaces so boundaries match cleanly
    norm = re.sub(r"[._\-+\[\]()]", " ", text)

    # Pattern 1: S01 E04, S1 E4, S01E04
    m = re.search(r"(?i)\bS(\d{1,2})\s*E(\d{1,3})\b", norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Pattern 2: 1x04, 01x04
    m = re.search(r"(?i)\b(\d{1,2})\s*x\s*(\d{1,3})\b", norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Pattern 3: Season 1 Episode 4, Season 01 Ep 4, Season 1 Ep 4
    m = re.search(r"(?i)\bSeason\s*(\d{1,2})\s*.*?Ep(?:isode)?\s*(\d{1,3})\b", norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Pattern 4: Standalone Episode (e.g. E04, EP04, Ep 4, Episode 4)
    m = re.search(r"(?i)\b(?:Ep(?:isode)?|E)\s*(\d{1,3})\b", norm)
    if m:
        return None, int(m.group(1))

    # Pattern 5: Part 4, Pt 4
    m = re.search(r"(?i)\b(?:Part|Pt)\s*(\d{1,3})\b", norm)
    if m:
        return None, int(m.group(1))

    return None, None


def tokenize_clean(text: str) -> set[str]:
    """Normalize and clean text into significant keyword tokens."""
    if not text:
        return set()
    
    # Replace separators with spaces
    cleaned = re.sub(r"[._\-\[\](){}+,;:!?'\"/\\]", " ", text.lower())
    words = cleaned.split()
    
    tokens = set()
    for w in words:
        w_clean = w.strip()
        if not w_clean or len(w_clean) < 2:
            continue
        if w_clean in _NOISE_WORDS:
            continue
        tokens.add(w_clean)
    return tokens


def calculate_match_score(video_title: str, video_filename: str, sub_filename: str, distance: int = 0) -> float:
    """Calculate a fuzzy confidence score (0 to 100+) between a video and a candidate subtitle file.
    
    Factors considered:
    1. Season & Episode agreement (highest weight for TV shows, hard rejection on mismatch)
    2. Shared keywords / token overlap
    3. String similarity ratio (SequenceMatcher)
    4. Distance in Telegram message IDs (proximity tie-breaker)
    """
    v_combined = f"{video_title} {video_filename}".lower()
    s_clean = sub_filename.lower()

    # 1. Season & Episode Matching
    v_s, v_e = extract_season_episode(v_combined)
    s_s, s_e = extract_season_episode(s_clean)

    score = 0.0

    if v_e is not None:
        if s_e is not None:
            if v_e == s_e:
                # Episodes match!
                score += 55.0
                if v_s is not None and s_s is not None:
                    if v_s == s_s:
                        score += 35.0  # Both season and episode match perfectly
                    else:
                        return -1000.0  # Wrong season (e.g. S01E04 vs S02E04)
            else:
                return -1000.0  # Wrong episode number (e.g. Ep 3 vs Ep 4) -> reject immediately!
        else:
            # Subtitle doesn't have an explicit episode tag, will rely on name matching
            score -= 10.0
    elif s_e is not None:
        # Video is not a series/episode, but sub is marked as episode -> mismatch
        score -= 20.0

    # 2. Token Overlap
    v_tokens = tokenize_clean(v_combined)
    s_tokens = tokenize_clean(s_clean)

    if v_tokens and s_tokens:
        overlap = v_tokens.intersection(s_tokens)
        union = v_tokens.union(s_tokens)
        jaccard = len(overlap) / max(len(union), 1)
        score += jaccard * 40.0
        
        # If all video tokens (or majority) are in the subtitle tokens
        subset_ratio = len(overlap) / max(len(v_tokens), 1)
        score += subset_ratio * 25.0

    # 3. String Fuzzy Similarity Ratio
    clean_v_str = " ".join(sorted(v_tokens))
    clean_s_str = " ".join(sorted(s_tokens))
    sim = difflib.SequenceMatcher(None, clean_v_str, clean_s_str).ratio()
    score += sim * 20.0

    # Direct substring bonus
    norm_title = re.sub(r"[^\w\s]", "", video_title.lower()).strip()
    norm_sub = re.sub(r"[^\w\s]", "", sub_filename.lower()).strip()
    if norm_title and (norm_title in norm_sub or norm_sub in norm_title):
        score += 25.0

    # 4. Proximity bonus (nearer messages get slight preference)
    score += max(0.0, 10.0 - (distance * 0.3))

    return score


async def fetch_from_yify(msg_id: int, imdb_id: str) -> str | None:
    """Query yifysubtitles.ch for the movie's English subtitle, extract and cache it."""
    if not imdb_id or not imdb_id.startswith("tt"):
        return None

    url = f"https://yifysubtitles.ch/movie-imdb/{imdb_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

                # Find English subtitle link
                matches = re.findall(r'href="(/subtitles/[^"]+-english-yify-[^"]+)"', html)
                if not matches:
                    matches = re.findall(r'href="(/subtitles/[^"]+)"[^>]*>.*?English', html, re.DOTALL | re.IGNORECASE)

                if not matches:
                    return None

                # Take the first matched English subtitle
                sub_page_slug = matches[0]
                sub_page_url = f"https://yifysubtitles.ch{sub_page_slug}"

                # Fetch subtitle page to get the ZIP download link
                async with sess.get(sub_page_url, headers=headers, timeout=10) as sub_resp:
                    if sub_resp.status != 200:
                        return None
                    sub_html = await sub_resp.text()

                    zip_matches = re.findall(r'href="(/subtitle/[^"]+\.zip)"', sub_html)
                    if not zip_matches:
                        slug_name = sub_page_slug.replace("/subtitles/", "")
                        zip_url = f"https://yifysubtitles.ch/subtitle/{slug_name}.zip"
                    else:
                        zip_url = f"https://yifysubtitles.ch{zip_matches[0]}"

                    # Download the ZIP file
                    async with sess.get(zip_url, headers=headers, timeout=15) as zip_resp:
                        if zip_resp.status != 200:
                            return None
                        zip_data = await zip_resp.read()

                        # Extract the SRT file from Zip
                        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                            for name in z.namelist():
                                if name.lower().endswith(".srt"):
                                    srt_content = z.read(name)
                                    cache_path = os.path.join(SUB_DIR, f"{msg_id}_en.srt")
                                    with open(cache_path, "wb") as f:
                                        f.write(srt_content)
                                    print(f"[subtitles] Scraped and saved English subtitles from Yify: {name}")
                                    return cache_path

    except Exception as e:
        print(f"[subtitles] Yify scraper error for imdb_id={imdb_id}: {e}")

    return None


async def fetch_english_subtitle(msg_id: int, title: str, filename: str) -> str | None:
    """Find and cache subtitles for the requested video.
    
    1. Checks local cache (.cache/subtitles/<msg_id>_en.srt).
    2. Searches neighboring Telegram messages in a wide window (+-25 messages)
       using fuzzy scoring & season/episode awareness.
    3. Falls back to online scrapers (Yify) via cached IMDb ID.
    """
    # 1. Local Cache Check
    cache_path = os.path.join(SUB_DIR, f"{msg_id}_en.srt")
    if os.path.exists(cache_path):
        return cache_path

    # 2. Telegram Scan for Subtitle Files (.srt, .vtt, .ass, .zip)
    try:
        from streaming import _get_entity
        client = _cfg.client
        if client:
            entity = await _get_entity()
            if entity:
                # Expanded window of +-25 messages to catch batch uploads
                start_id = max(1, msg_id - 25)
                end_id = msg_id + 26
                ids = list(range(start_id, end_id))
                
                messages = await client.get_messages(entity, ids=ids)
                
                candidates: list[tuple[float, any, str]] = []  # (score, msg, filename)

                for msg in messages:
                    if not msg or not msg.media or not isinstance(msg.media, MessageMediaDocument):
                        continue

                    doc_name = ""
                    for attr in msg.media.document.attributes:
                        if isinstance(attr, DocumentAttributeFilename):
                            doc_name = attr.file_name
                            break

                    if not doc_name:
                        continue

                    ext = os.path.splitext(doc_name)[1].lower()
                    if ext in (".srt", ".vtt", ".ass", ".sub", ".zip"):
                        dist = abs(msg.id - msg_id)
                        score = calculate_match_score(title, filename, doc_name, distance=dist)
                        
                        if score > 15.0:  # Valid candidate threshold
                            candidates.append((score, msg, doc_name))

                if candidates:
                    # Sort by highest score first
                    candidates.sort(key=lambda x: x[0], reverse=True)
                    best_score, best_msg, best_doc_name = candidates[0]
                    
                    print(f"[subtitles] Best match for '{title}' (score={best_score:.1f}): {best_doc_name} (msg_id={best_msg.id})")
                    
                    temp_path = cache_path + ".tmp"
                    await client.download_media(best_msg, file=temp_path)

                    ext = os.path.splitext(best_doc_name)[1].lower()

                    # If it's a zip file containing srt
                    if ext == ".zip":
                        extracted = False
                        try:
                            with zipfile.ZipFile(temp_path, "r") as z:
                                srt_files = [n for n in z.namelist() if n.lower().endswith(".srt") or n.lower().endswith(".vtt")]
                                if srt_files:
                                    # Pick best inside zip
                                    zip_best = srt_files[0]
                                    content = z.read(zip_best)
                                    with open(cache_path, "wb") as f:
                                        f.write(content)
                                    extracted = True
                        except Exception as ze:
                            print(f"[subtitles] Failed to extract zip {best_doc_name}: {ze}")
                        
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        if extracted:
                            return cache_path

                    # If it's a WebVTT file, convert to standard format
                    elif ext == ".vtt":
                        with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        content = re.sub(r"^WEBVTT\s*\n", "", content)
                        with open(cache_path, "w", encoding="utf-8") as f:
                            f.write(content)
                        os.remove(temp_path)
                        return cache_path

                    # Standard SRT / ASS / text subtitle
                    else:
                        if os.path.exists(cache_path):
                            os.remove(cache_path)
                        os.rename(temp_path, cache_path)
                        return cache_path

    except Exception as e:
        print(f"[subtitles] Error fetching Telegram subtitle for {msg_id}: {e}")

    # 3. Fallback: Keyless Online Search (YifySubtitles via IMDb ID)
    try:
        import cache as _cache
        video = _cache._cache_meta.get(msg_id, {})
        album_name = video.get("album", "")
        pm = _cache._poster_mem.get(album_name, {})
        meta = pm.get("meta", {})
        imdb_id = meta.get("imdb_id")

        if imdb_id:
            path = await fetch_from_yify(msg_id, imdb_id)
            if path:
                return path
    except Exception as e:
        print(f"[subtitles] Fallback online subtitle search error: {e}")

    return None
