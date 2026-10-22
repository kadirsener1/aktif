import aiohttp
from aiohttp import web
import asyncio
import json
import os
import re
import time

CONFIG_FILE = "channels.json"

# Hafıza (Cache) yapısı
STREAM_CACHE = {}
CACHE_DURATION = 600  # 10 dakika

def load_channels():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

async def check_url(session, url):
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
                        max_res_score = 921600
                    
                    stability_score = max_res_score / (1.0 + (elapsed_time * 2.0))
                    return (url, stability_score)
    except Exception:
        pass
    return None

async def get_working_stream(channel_name):
    current_time = time.time()
    
    if channel_name in STREAM_CACHE:
        cached_data = STREAM_CACHE[channel_name]
        if current_time - cached_data["time"] < CACHE_DURATION:
            return cached_data["url"]

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

async def dashboard(request):
    """Tüm kanalların durumunu ve aktif linklerini gösteren web paneli."""
    channels = load_channels()
    current_time = time.time()
    
    html = """
    <!DOCTYPE html>
    <html lang="tr">
    <head>
        <meta charset="UTF-8">
        <title>IPTV Failover Proxy Dashboard</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
            .container { max-width: 1100px; margin: 0 auto; }
            h1 { color: #38bdf8; font-size: 24px; margin-bottom: 5px; }
            p.subtitle { color: #94a3b8; margin-top: 0; }
            table { width: 100%; border-collapse: collapse; margin-top: 20px; background: #1e293b; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
            th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; }
            th { background: #0f172a; color: #38bdf8; font-weight: 600; }
            tr:last-child td { border-bottom: none; }
            .badge-cached { background: #1e3a8a; color: #60a5fa; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 500; }
            .badge-inactive { background: #334155; color: #94a3b8; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 500; }
            a { color: #38bdf8; text-decoration: none; }
            a:hover { text-decoration: underline; }
            .btn-refresh { background: #0284c7; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: 500; }
            .btn-refresh:hover { background: #0369a1; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>IPTV Smart Proxy Paneli</h1>
            <p class="subtitle">Sistemde tanımlı kanalların anlık durumu ve aktif yayın linkleri</p>
            <button class="btn-refresh" onclick="location.reload()">Sayfayı Yenile</button>
            <table>
                <thead>
                    <tr>
                        <th>Kanal Adı</th>
                        <th>Durum / Önbellek</th>
                        <th>Oynatıcı Linki & Seçilen Kaynak</th>
                    </tr>
                </thead>
                <tbody>
    """
    
    for name, sources in channels.items():
        play_link = f"/play/{name}"
        cache_info = STREAM_CACHE.get(name)
        
        if cache_info and (current_time - cache_info["time"] < CACHE_DURATION):
            status_html = '<span class="badge-cached">Önbellekte (Aktif)</span>'
            active_url = cache_info["url"]
        else:
            status_html = '<span class="badge-inactive">Test Bekliyor</span>'
            active_url = "Kanal ilk açıldığında test edilip seçilecek"
        
        html += f"""
                    <tr>
                        <td><strong>{name}</strong></td>
                        <td>{status_html}</td>
                        <td><a href="{play_link}" target="_blank">{play_link}</a><br><small style="color: #94a3b8; word-break: break-all;">{active_url}</small></td>
                    </tr>
        """
        
    html += """
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

app = web.Application()
app.router.add_get('/play/{channel_name}', handle_stream)
app.router.add_get('/', dashboard)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, port=port)
