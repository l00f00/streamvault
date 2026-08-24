import os
import asyncio
import re
import zipfile
import io
import aiohttp
import config as _cfg
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename

# Subtitles cache directory
_here = os.path.dirname(os.path.abspath(__file__))
SUB_DIR = os.path.join(_here, ".cache", "subtitles")
os.makedirs(SUB_DIR, exist_ok=True)

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
    """Check for local cache, download Telegram attached subtitles, scrape online subtitles, or return None.
    
    Checks in .cache/subtitles/ first. Then checks neighboring Telegram messages
    (msg_id - 5 to msg_id + 5). Falls back to YifySubtitles search using cached IMDb ID.
    """
    # 1. Local Cache Check
    cache_path = os.path.join(SUB_DIR, f"{msg_id}_en.srt")
    if os.path.exists(cache_path):
        return cache_path

    # 2. Telegram Scan for Neighboring Subtitle Files
    try:
        from streaming import _get_entity
        client = _cfg.client
        if client:
            entity = await _get_entity()
            if entity:
                # Fetch messages around the video msg_id
                ids = list(range(max(1, msg_id - 5), msg_id + 6))
                messages = await client.get_messages(entity, ids=ids)
                
                target_msg = None
                clean_title = re.sub(r"[^\w\s]", "", title.lower()).strip()
                
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
                    if ext in (".srt", ".vtt"):
                        clean_doc = re.sub(r"[^\w\s]", "", doc_name.lower()).strip()
                        if clean_title in clean_doc or clean_doc in clean_title or len(messages) <= 3:
                            target_msg = msg
                            break
                            
                if target_msg:
                    print(f"[subtitles] Found matching Telegram subtitle file: {doc_name}")
                    temp_path = cache_path + ".tmp"
                    await client.download_media(target_msg, file=temp_path)
                    
                    if doc_name.lower().endswith(".vtt"):
                        with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        content = re.sub(r"^WEBVTT\s*\n", "", content)
                        with open(cache_path, "w", encoding="utf-8") as f:
                            f.write(content)
                        os.remove(temp_path)
                    else:
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
