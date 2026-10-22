import aiohttp
from aiohttp import web
import asyncio
import json
import os
import re
import time

CONFIG_FILE = "channels.json"

# Hafıza (Cache) yapısı: { "kanal_adi": { "url": "çalışan_link", "time": zaman_damgasi } }
STREAM_CACHE = {}
CACHE_DURATION = 600  # 10 dakika boyunca aynı linki tekrar test etmez, hafızadan sunar

def load_channels():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

async def check_url(session, url):
    """M3U8 linkini test eder; 2 saniye içinde yanıt vermezse eler."""
    start_time = time.time()
    try:
        async with session.get(url, timeout=2.0) as response:
            elapsed_time = time.time() - start_time
            
            if response.status == 200:
                content = await response.text()
                if "#EXTM3U" in content:
                    max_res_score = 0
                    resolutions = re.findall(r'RESOLUTION=(\d+)x(\d+)', content)
                    
                    if resolutions:
                        for w, h in resolutions:
                            score = int(w) * int(h)
                            if score > max_res_score:
                                max_res_score = score
                    else:
                        max_res_score = 921600 # 720p taban skor
                    
                    stability_score = max_res_score / (1.0 + (elapsed_time * 2.0))
                    return (url, stability_score)
    except Exception:
        pass
    return None

async def get_working_stream(channel_name):
    current_time = time.time()
    
    # 1. Önce Hafızaya (Cache) Bak: Eğer kanal daha önce test edilmişse ve 10 dakika geçmemişse direkt döndür
    if channel_name in STREAM_CACHE:
        cached_data = STREAM_CACHE[channel_name]
        if current_time - cached_data["time"] < CACHE_DURATION:
            return cached_data["url"]

    # 2. Hafızada yoksa veya süresi dolmuşsa yeni baştan test et
    channels = load_channels()
    sources = channels.get(channel_name)
    if not sources:
        return None
        
    async with aiohttp.ClientSession() as session:
        tasks = [check_url(session, url) for url in sources]
        results = await asyncio.gather(*tasks)
        
        working_streams = [res for res in results if res is not None]
        if not working_streams:
            return None
        
        working_streams.sort(key=lambda x: x[1], reverse=True)
        best_url = working_streams[0][0]
        
        # 3. Bulunan en iyi linki hafızaya kaydet
        STREAM_CACHE[channel_name] = {
            "url": best_url,
            "time": current_time
        }
        
        return best_url

async def handle_stream(request):
    channel_name = request.match_info.get('channel_name')
    channels = load_channels()
    
    if channel_name not in channels:
        return web.Response(text="Böyle bir kanal tanımlı değil.", status=404)
        
    working_url = await get_working_stream(channel_name)
    
    if not working_url:
        return web.Response(text=f"{channel_name} için şu an aktif yayın bulunamadı.", status=503)
    
    raise web.HTTPFound(working_url)

async def health_check(request):
    return web.Response(text="Cached Multi-Channel Proxy Çalışıyor")

app = web.Application()
app.router.add_get('/play/{channel_name}', handle_stream)
app.router.add_get('/', health_check)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, port=port)
