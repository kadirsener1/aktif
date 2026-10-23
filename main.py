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
CACHE_DURATION = 60  # 1 dakika cache

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "iptv123")

def load_channels():
    local_channels = {}
    github_channels = {}

    # 1. Önce yerel dosyayı oku (varsa)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                local_channels = json.load(f)
                if not isinstance(local_channels, dict):
                    local_channels = {}
        except Exception:
            pass

    # 2. GitHub'dan en güncel veriyi çekmeye çalış
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            import requests
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CONFIG_FILE}"
            headers = {
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json"
            }
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                file_data = res.json()
                if "content" in file_data:
                    decoded_content = base64.b64decode(file_data["content"]).decode("utf-8")
                    github_channels = json.loads(decoded_content)
                    if not isinstance(github_channels, dict):
                        github_channels = {}
        except Exception as e:
            print(f"GitHub Load Error: {e}")

    # 3. İki kaynağı birleştir (Merge): GitHub ve Yerel verileri harmanla (Veri kaybını önlemek için)
    # GitHub önceliklidir, ancak yerelde olup GitHub'da olmayan yeni eklemeler silinmez.
    merged_channels = {**local_channels, **github_channels}

    # Birleştirilmiş güncel hali yerel diske de yazıp eşitleyelفظ
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(merged_channels, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

    return merged_channels

def save_and_push_channels(channels):
    try:
        # Önce mevcut GitHub verisini çekip üzerine güvenli ekleme yapalım (Veri kaybı olmasın)
        current_channels = load_channels()
        # Gönderilen güncel sözlüğü mevcutla güncelle
        current_channels.update(channels)

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(current_channels, f, ensure_ascii=False, indent=4)
        
        if GITHUB_TOKEN and GITHUB_REPO:
            import requests
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CONFIG_FILE}"
            headers = {
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json"
            }
            res = requests.get(url, headers=headers)
            sha = res.json().get("sha") if res.status_code == 200 else None
            
            content_str = json.dumps(current_channels, ensure_ascii=False, indent=4)
            content_encoded = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
            
            data = {
                "message": "Admin panelinden channels.json güncellendi ve senkronize edildi",
                "content": content_encoded,
                "sha": sha
            }
            if not sha:
                data.pop("sha")
            requests.put(url, headers=headers, json=data)
    except Exception as e:
        print(f"GitHub Sync Error: {e}")

def check_auth(request):
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return False
    try:
        auth_type, encoded = auth_header.split(' ')
        if auth_type.lower() == 'basic':
            decoded = base64.b64decode(encoded).decode('utf-8')
            username, password = decoded.split(':', 1)
            return username == ADMIN_USER and password == ADMIN_PASS
    except Exception:
        pass
    return False

def basic_auth_required(handler):
    async def wrapper(request):
        if not check_auth(request):
            return web.Response(
                status=401,
                headers={'WWW-Authenticate': 'Basic realm="IPTV Admin Paneli"'},
                text="Yetkisiz erişim. Lütfen giriş yapın."
            )
        return await handler(request)
    return wrapper

async def check_url(session, url):
    start_time = time.time()
    try:
        async with session.get(url, timeout=2.0) as response:
            elapsed_time = time.time() - start_time
            if response.status == 200:
                content = await response.text()
                if "#EXTM3U" in content:
                    max_height = 0
                    resolutions = re.findall(r'RESOLUTION=(\d+)x(\d+)', content)
                    if resolutions:
                        for w, h in resolutions:
                            h_val = int(h)
                            if h_val > max_height:
                                max_height = h_val
                    else:
                        max_height = 720  # Çözünürlük bilgisi yoksa orta kalite kabul edilir
                    
                    # Tüm izleyicileri koruyan hız ve stabilite dengeli formül
                    stability_score = max_height / (elapsed_time * 3.0 + 0.1)
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

async def ping(request):
    return web.Response(text="OK", status=200)

async def handle_stream(request):
    channel_name = request.match_info.get('channel_name')
    channels = load_channels()
    
    if channel_name not in channels:
        return web.Response(text="Böyle bir kanal tanımlı değil.", status=404)
        
    working_url = await get_working_stream(channel_name)
    if not working_url:
        return web.Response(text=f"{channel_name} için şu an aktif yayın bulunamadı.", status=503)
    
    raise web.HTTPFound(working_url)

@basic_auth_required
async def admin_save(request):
    data = await request.post()
    old_name = data.get('old_name', '').strip().lower()
    channel_name = data.get('channel_name', '').strip().lower()
    urls_raw = data.get('urls', '').strip()
    
    if channel_name and urls_raw:
        urls = [u.strip() for u in urls_raw.replace(',', '\n').split('\n') if u.strip()]
        channels = load_channels()
        
        if old_name and old_name != channel_name and old_name in channels:
            del channels[old_name]
            if old_name in STREAM_CACHE:
                del STREAM_CACHE[old_name]
        
        channels[channel_name] = urls
        save_and_push_channels(channels)
        
        if channel_name in STREAM_CACHE:
            del STREAM_CACHE[channel_name]
            
    raise web.HTTPFound('/')

@basic_auth_required
async def admin_delete(request):
    channel_name = request.match_info.get('channel_name')
    channels = load_channels()
    
    if channel_name in channels:
        del channels[channel_name]
        save_and_push_channels(channels)
        if channel_name in STREAM_CACHE:
            del STREAM_CACHE[channel_name]
            
    raise web.HTTPFound('/')

@basic_auth_required
async def admin_clear_cache(request):
    STREAM_CACHE.clear()
    raise web.HTTPFound('/')

@basic_auth_required
async def dashboard(request):
    channels = load_channels()
    current_time = time.time()
    channels_json_esc = json.dumps(channels, ensure_ascii=False)
    
    html = f"""
    <!DOCTYPE html>
    <html lang="tr">
    <head>
        <meta charset="UTF-8">
        <title>IPTV Failover Admin Paneli</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 1100px; margin: 0 auto; }}
            h1 {{ color: #38bdf8; font-size: 24px; margin-bottom: 5px; }}
            p.subtitle {{ color: #94a3b8; margin-top: 0; }}
            .card {{ background: #1e293b; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }}
            input, textarea {{ width: 100%; padding: 10px; margin: 6px 0 12px 0; background: #0f172a; border: 1px solid #334155; color: white; border-radius: 4px; box-sizing: border-box; }}
            button, .btn-action {{ background: #0284c7; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-weight: 500; text-decoration: none; display: inline-block; }}
            button:hover, .btn-action:hover {{ background: #0369a1; }}
            .btn-danger {{ background: #dc2626; padding: 6px 12px; font-size: 12px; }}
            .btn-danger:hover {{ background: #b91c1c; }}
            .btn-warning {{ background: #d97706; padding: 6px 12px; font-size: 12px; margin-right: 5px; }}
            .btn-warning:hover {{ background: #b45309; }}
            .btn-clear {{ background: #0d9488; margin-bottom: 15px; }}
            .btn-clear:hover {{ background: #0f766e; }}
            .btn-secondary {{ background: #475569; padding: 6px 12px; font-size: 12px; margin-left: 10px; display: none; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; }}
            th {{ background: #0f172a; color: #38bdf8; font-weight: 600; }}
            tr:last-child td {{ border-bottom: none; }}
            .badge-cached {{ background: #1e3a8a; color: #60a5fa; padding: 4px 8px; border-radius: 4px; font-size: 12px; }}
            .badge-inactive {{ background: #334155; color: #94a3b8; padding: 4px 8px; border-radius: 4px; font-size: 12px; }}
            a {{ color: #38bdf8; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
        </style>
        <script>
            const allChannels = {channels_json_esc};
            
            function editChannel(name) {{
                const urls = allChannels[name];
                if(urls) {{
                    document.getElementById('old_name').value = name;
                    document.getElementById('channel_name').value = name;
                    document.getElementById('urls').value = urls.join('\\n');
                    document.getElementById('form-title').innerText = "Kanalı Düzenle: " + name;
                    document.getElementById('submit-btn').innerText = "Değişiklikleri Kaydet";
                    document.getElementById('cancel-btn').style.display = "inline-block";
                    window.scrollTo({{ top: 0, behavior: 'smooth' }});
                }}
            }}
            
            function resetForm() {{
                document.getElementById('old_name').value = "";
                document.getElementById('channel_name').value = "";
                document.getElementById('urls').value = "";
                document.getElementById('form-title').innerText = "Yeni Kanal Ekle";
                document.getElementById('submit-btn').innerText = "Kanalı Ekle ve GitHub'a Kaydet";
                document.getElementById('cancel-btn').style.display = "none";
            }}
            
            function filterChannels() {{
                const input = document.getElementById('searchInput').value.toLowerCase();
                const table = document.getElementById('channelTable');
                const trs = table.getElementsByTagName('tr');
                
                for (let i = 1; i < trs.length; i++) {{
                    const td = trs[i].getElementsByTagName('td')[0];
                    if (td) {{
                        const textValue = td.textContent || td.innerText;
                        if (textValue.toLowerCase().indexOf(input) > -1) {{
                            trs[i].style.display = "";
                        }} else {{
                            trs[i].style.display = "none";
                        }}
                    }}
                }}
            }}
        </script>
    </head>
    <body>
        <div class="container">
            <h1>IPTV Smart Proxy Admin Paneli</h1>
            <p class="subtitle">Arama çubuğu ve akıllı stabilite algoritması aktif.</p>
            
            <a href="/admin/clear-cache" class="btn-action btn-clear" onclick="return confirm('Tüm önbellek (cache) temizlensin mi?')">🧹 Önbelleği (Cache) Anında Temizle</a>

            <div class="card">
                <h3 id="form-title">Yeni Kanal Ekle</h3>
                <form action="/admin/save" method="POST">
                    <input type="hidden" id="old_name" name="old_name" value="">
                    
                    <label>Kanal Adı (Örn: showtv, sportv):</label>
                    <input type="text" id="channel_name" name="channel_name" required placeholder="sportv">
                    
                    <label>M3U8 Linkleri (Alt alta veya virgülle ayırarak yazın):</label>
                    <textarea id="urls" name="urls" rows="3" required placeholder="https://ornek.com/kanal1.m3u8&#10;https://ornek.com/kanal2.m3u8"></textarea>
                    
                    <button type="submit" id="submit-btn">Kanalı Ekle ve GitHub'a Kaydet</button>
                    <button type="button" id="cancel-btn" class="btn-secondary" onclick="resetForm()">İptal</button>
                </form>
            </div>

            <div class="card">
                <h3>Tanımlı Kanallar Listesi</h3>
                <input type="text" id="searchInput" placeholder="🔍 Kanal adıyla hızlıca ara..." onkeyup="filterChannels()">
                <table id="channelTable">
                    <thead>
                        <tr>
                            <th>Kanal Adı</th>
                            <th>Durum</th>
                            <th>Oynatıcı Linki & Seçilen Kaynak</th>
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
                            <td>
                                <button class="btn-warning" onclick="editChannel('{name}')">Düzenle</button>
                                <a href="{delete_link}" class="btn-danger" style="color:white; padding: 6px 10px; border-radius:4px; display:inline-block;" onclick="return confirm('Bu kanalı silmek istediğine emin misin?')">Sil</a>
                            </td>
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
app.router.add_post('/admin/save', admin_save)
app.router.add_get('/admin/delete/{channel_name}', admin_delete)
app.router.add_get('/admin/clear-cache', admin_clear_cache)
app.router.add_get('/ping', ping)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, port=port)
