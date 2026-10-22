import aiohttp
from aiohttp import web
import asyncio
import json
import os

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
    """M3U8 linkinin çalışıp çalışmadığını test eder."""
    try:
        async with session.get(url, timeout=3.0) as response:
            if response.status == 200:
                content = await response.text()
                if "#EXTM3U" in content:
                    return url
    except Exception:
        pass
    return None

async def get_working_stream(channel_name):
    """Güncel JSON verisinden kanal kaynaklarını test eder."""
    channels = load_channels()
    sources = channels.get(channel_name)
    if not sources:
        return None
        
    async with aiohttp.ClientSession() as session:
        tasks = [check_url(session, url) for url in sources]
        results = await asyncio.gather(*tasks)
        
        for working_url in results:
            if working_url:
                return working_url
    return None

async def handle_stream(request):
    """Kanal adına göre çalışan yayına yönlendirir."""
    channel_name = request.match_info.get('channel_name')
    channels = load_channels()
    
    if channel_name not in channels:
        return web.Response(text="Böyle bir kanal tanımlı değil.", status=404)
        
    working_url = await get_working_stream(channel_name)
    
    if not working_url:
        return web.Response(text=f"{channel_name} için şu an aktif yayın bulunamadı.", status=503)
    
    raise web.HTTPFound(working_url)

async def health_check(request):
    return web.Response(text="JSON Destekli Failover Proxy Çalışıyor")

app = web.Application()
app.router.add_get('/play/{channel_name}', handle_stream)
app.router.add_get('/', health_check)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, port=port)
