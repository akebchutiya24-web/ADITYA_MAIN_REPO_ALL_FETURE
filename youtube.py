import os
import asyncio
import aiohttp
from youtube_search import YoutubeSearch

API_URL = os.environ.get("API_URL", "https://api.shrutibots.site")
API_KEY = os.environ.get("API_KEY", "")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# /vplay ke liye — bilkul audio wali ShrutiAPI hi hai, bas host thoda alag
# (koi "api." subdomain nahi) aur type=video jaata hai.
VIDEO_API_URL = os.environ.get("VIDEO_API_URL", "https://shrutibots.site")
VIDEO_API_KEY = os.environ.get("VIDEO_API_KEY", "ShrutiBotsh9i11kGGDP3GB0LP018U")


async def search_youtube(query: str):
    """YouTube pe search karta hai, pehla result deta hai (raw dict, library format)."""
    loop = asyncio.get_event_loop()

    def _search():
        results = YoutubeSearch(query, max_results=1).to_dict()
        return results[0] if results else None

    return await loop.run_in_executor(None, _search)


async def search_track(query: str):
    """search_youtube ka normalized wrapper — id/title/duration/thumbnail/url deta hai."""
    result = await search_youtube(query)
    if not result:
        return None

    thumbnails = result.get("thumbnails") or []
    video_id = result.get("id")

    return {
        "id": video_id,
        "title": result.get("title", "Unknown"),
        "duration": result.get("duration", ""),
        "thumbnail": thumbnails[0] if thumbnails else None,
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
    }


async def search_related_track(seed_title: str, exclude_ids=None):
    """Autoplay ke liye 'related' track dhoondta hai — seed track ke title se
    hi search karke, pehli aisi result jo already play na ho chuki ho (exclude_ids
    mein na ho) use karta hai. YouTube ka koi official 'related videos' API
    yahan use nahi ho raha (youtube_search library isse support nahi karti),
    isliye yeh best-effort approximation hai."""
    exclude_ids = exclude_ids or set()
    loop = asyncio.get_event_loop()

    def _search():
        results = YoutubeSearch(seed_title, max_results=6).to_dict()
        return results

    results = await loop.run_in_executor(None, _search)
    for r in results or []:
        vid = r.get("id")
        if vid and vid not in exclude_ids:
            thumbnails = r.get("thumbnails") or []
            return {
                "id": vid,
                "title": r.get("title", "Unknown"),
                "duration": r.get("duration", ""),
                "thumbnail": thumbnails[0] if thumbnails else None,
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
    return None


async def get_stream_url(video_id: str) -> str:
    """
    ShrutiAPI se direct audio stream URL nikalta hai.
    Koi file download nahi hoti — seedha URL milta hai jo pytgcalls stream karta hai.
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{API_URL}/download",
            params={"url": video_id, "type": "audio", "api_key": API_KEY},
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=False,  # redirect URL chahiye, file nahi
        ) as resp:
            # Agar API direct stream URL redirect kare
            if resp.status in (301, 302, 303, 307, 308):
                return resp.headers.get("Location")

            # Agar API JSON mein URL deta hai
            if resp.content_type and "json" in resp.content_type:
                data = await resp.json()
                url = (
                    data.get("url")
                    or data.get("download_url")
                    or data.get("link")
                    or data.get("audio_url")
                )
                if url:
                    return url

            # Fallback: agar API seedha file stream karta hai to download karo
            if resp.status == 200:
                file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
                with open(file_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(131072):
                        f.write(chunk)
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    return file_path

            raise Exception(f"API ne unexpected response diya: {resp.status}")


async def get_video_stream_url(video_id: str) -> str:
    """
    ShrutiAPI (video variant) se direct video stream URL nikalta hai — /vplay
    ke liye. Bilkul get_stream_url() jaisa hi hai — bas type=video, aur
    poora link nahi, sirf YouTube video ID chahiye (jaise 'GX9x62kFsVU').
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{VIDEO_API_URL}/download",
            params={"url": video_id, "type": "video", "api_key": VIDEO_API_KEY},
            timeout=aiohttp.ClientTimeout(total=360),  # video processing dheere hota hai
            allow_redirects=False,
        ) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                return resp.headers.get("Location")

            if resp.content_type and "json" in resp.content_type:
                data = await resp.json()
                url = (
                    data.get("url")
                    or data.get("download_url")
                    or data.get("link")
                    or data.get("video_url")
                )
                if url:
                    return url

            if resp.status == 200:
                file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
                with open(file_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(131072):
                        f.write(chunk)
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    return file_path

            raise Exception(f"Video API ne unexpected response diya: {resp.status}")
