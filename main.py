import aiohttp
from aiohttp import web
import asyncio
import json
import os
import re

CONFIG_FILE = "channels.json"

def load_channels():
    """Kanal listesini JSON dosyasından okur."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

async def check_url(session, url):
    """M3U8 linkini test eder, çalışıyorsa çözünürlük skorunu hesaplar."""
    try:
        async with session.get(url, timeout=4.0) as response:
            if response.status == 200:
                content = await response.text()
                if "#EXTM3U" in content:
                    max_res_score = 0
                    # M3U8 içeriğindeki çözünürlük etiketlerini arar (Örn: RESOLUTION=1920x1080)
                    resolutions = re.findall(r'RESOLUTION=(\d+)x(\d+)', content)
                    if resolutions:
                        for w, h in resolutions:
                            score = int(w) * int(h)
                            if score > max_res_score:
                                max_res_score = score
                    else:
                        # Eğer master playlist değilse ve çözünürlük etiketi yoksa standart taban skor verilir
                        max_res_score = 100 
                    
                    return (url, max_res_score)
    except Exception:
        pass
    return None

async def get_working_stream(channel_name):
    """Çalışan linkler arasından çözünürlüğü en yüksek olanı seçer."""
    channels = load_channels()
    sources = channels.get(channel_name)
    if not sources:
        return None
        
    async with aiohttp.ClientSession() as session:
        tasks = [check_url(session, url) for url in sources]
        results = await asyncio.gather(*tasks)
        
        # Sadece çalışan (None olmayan) linkleri filtrele
        working_streams = [res for res in results if res is not None]
        
        if not working_streams:
            return None
        
        # Çözünürlük skoruna göre büyükten küçüğe sırala (En yüksek çözünürlük ilk sıraya gelir)
        working_streams.sort(key=lambda x: x[1], reverse=True)
        
        # En yüksek çözünürlüklü linki döndür
        return working_streams[0][0]

async def handle_stream(request):
    """Kanal adına göre en yüksek çözünürlüklü çalışan yayına yönlendirir."""
    channel_name = request.match_info.get('channel_name')
    channels = load_channels()
    
    if channel_name not in channels:
        return web.Response(text="Böyle bir kanal tanımlı değil.", status=404)
        
    working_url = await get_working_stream(channel_name)
    
    if not working_url:
        return web.Response(text=f"{channel_name} için şu an aktif yayın bulunamadı.", status=503)
    
    raise web.HTTPFound(working_url)

async def health_check(request):
    return web.Response(text="Resolution-Aware Failover Proxy Çalışıyor")

app = web.Application()
app.router.add_get('/play/{channel_name}', handle_stream)
app.router.add_get('/', health_check)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, port=port)
