import aiohttp
from aiohttp import web
import asyncio
import json
import os
import re
import time
import base64

CONFIG_FILE = "channels.json"
STREAM_CACHE = {}
CACHE_DURATION = 600  # 10 dakika

# GitHub Entegrasyon Bilgileri (Render Environment Variables'dan okunur)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # Örn: "kullaniciadi/repo-adi"

def load_channels():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_and_push_channels(channels):
    """Yerel dosyayı günceller ve GitHub API kullanarak repoya kaydeder."""
    try:
        # 1. Yerel dosyayı kaydet
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(channels, f, ensure_ascii=False, indent=4)
        
        # 2. Eğer GitHub bilgileri tanımlıysa, repodaki channels.json dosyasını güncelle
        if GITHUB_TOKEN and GITHUB_REPO:
            import requests
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CONFIG_FILE}"
            headers = {
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json"
            }
            
            # Önce dosyanın mevcut SHA değerini almamız gerekiyor (GitHub API şartı)
            res = requests.get(url, headers=headers)
            sha = res.json().get("sha") if res.status_code == 200 else None
            
            # İçeriği Base64 formatına çevir
            content_str = json.dumps(channels, ensure_ascii=False, indent=4)
            content_encoded = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
            
            data = {
                "message": "Admin panelinden otomatik channels.json güncellemesi",
                "content": content_encoded,
                "sha": sha
            }
            if not sha:
                data.pop("sha")
                
            requests.put(url, headers=headers, json=data)
    except Exception as e:
        print(f"GitHub Sync Error: {e}")

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

async def admin_add(request):
    data = await request.post()
    channel_name = data.get('channel_name', '').strip().lower()
    urls_raw = data.get('urls', '').strip()
    
    if channel_name and urls_raw:
        urls = [u.strip() for u in urls_raw.replace(',', '\n').split('\n') if u.strip()]
        
        channels = load_channels()
        channels[channel_name] = urls
        save_and_push_channels(channels)  # Hem yerel hem GitHub güncellenir
        
        if channel_name in STREAM_CACHE:
            del STREAM_CACHE[channel_name]
            
    raise web.HTTPFound('/')

async def admin_delete(request):
    channel_name = request.match_info.get('channel_name')
    channels = load_channels()
    
    if channel_name in channels:
        del channels[channel_name]
        save_and_push_channels(channels)  # Hem yerel hem GitHub güncellenir
        if channel_name in STREAM_CACHE:
            del STREAM_CACHE[channel_name]
            
    raise web.HTTPFound('/')

async def dashboard(request):
    channels = load_channels()
    current_time = time.time()
    
    html = """
    <!DOCTYPE html>
    <html lang="tr">
    <head>
        <meta charset="UTF-8">
        <title>IPTV Failover Admin Paneli</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
            .container { max-width: 1100px; margin: 0 auto; }
            h1 { color: #38bdf8; font-size: 24px; margin-bottom: 5px; }
            p.subtitle { color: #94a3b8; margin-top: 0; }
            .card { background: #1e293b; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
            input, textarea { width: 100%; padding: 10px; margin: 6px 0 12px 0; background: #0f172a; border: 1px solid #334155; color: white; border-radius: 4px; box-sizing: border-box; }
            button { background: #0284c7; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-weight: 500; }
            button:hover { background: #0369a1; }
            .btn-danger { background: #dc2626; padding: 6px 12px; font-size: 12px; }
            .btn-danger:hover { background: #b91c1c; }
            table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; }
            th { background: #0f172a; color: #38bdf8; font-weight: 600; }
            tr:last-child td { border-bottom: none; }
            .badge-cached { background: #1e3a8a; color: #60a5fa; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
            .badge-inactive { background: #334155; color: #94a3b8; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
            a { color: #38bdf8; text-decoration: none; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>IPTV Smart Proxy Admin Paneli</h1>
            <p class="subtitle">Yaptığınız ekleme/silme işlemleri doğrudan GitHub'daki channels.json dosyasına kaydedilir.</p>
            
            <div class="card">
                <h3>Yeni Kanal Ekle</h3>
                <form action="/admin/add" method="POST">
                    <label>Kanal Adı (Örn: showtv, sportv):</label>
                    <input type="text" name="channel_name" required placeholder="sportv">
                    
                    <label>M3U8 Linkleri (Alt alta veya virgülle ayırarak yazın):</label>
                    <textarea name="urls" rows="3" required placeholder="https://ornek.com/kanal1.m3u8&#10;https://ornek.com/kanal2.m3u8"></textarea>
                    
                    <button type="submit">Kanalı Ekle ve GitHub'a Kaydet</button>
                </form>
            </div>

            <div class="card">
                <h3>Tanımlı Kanallar Listesi</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Kanal Adı</th>
                            <th>Durum</th>
                            <th>Oynatıcı Linki & Aktif Kaynak</th>
                            <th>İşlem</th>
                        </tr>
                    </thead>
                    <tbody>
    """
    
    for name, sources in channels.items():
        play_link = f"/play/{name}"
        delete_link = f"/admin/delete/{name}"
        cache_info = STREAM_CACHE.get(name)
        
        if cache_info and (current_time - cache_info["time"] < CACHE_DURATION):
            status_html = '<span class="badge-cached">Önbellekte Aktif</span>'
            active_url = cache_info["url"]
        else:
            status_html = '<span class="badge-inactive">Test Bekliyor</span>'
            active_url = "İlk açılışta test edilecek"
        
        html += f"""
                        <tr>
                            <td><strong>{name}</strong></td>
                            <td>{status_html}</td>
                            <td><a href="{play_link}" target="_blank">{play_link}</a><br><small style="color: #94a3b8; word-break: break-all;">{active_url}</small></td>
                            <td><a href="{delete_link}" class="btn-danger" style="color:white; padding: 6px 10px; border-radius:4px; display:inline-block;" onclick="return confirm('Bu kanalı silmek istediğine emin misin?')">Sil</a></td>
                        </tr>
        """
        
    html += """
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

app = web.Application()
app.router.add_get('/play/{channel_name}', handle_stream)
app.router.add_get('/', dashboard)
app.router.add_post('/admin/add', admin_add)
app.router.add_get('/admin/delete/{channel_name}', admin_delete)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, port=port)
