from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import asyncio
import json
import os
import time
import base64
from html import escape
import httpx
import secrets
import random

app = FastAPI()

CONFIG_FILE = "channels1.json"
_CHANNELS_CACHE = {"data": None, "time": 0}
CHANNELS_CACHE_TTL = 30

# Cloudflare Worker Sunucularınız
WORKER_SERVERS = [
    "https://vavoo1.magnitude.workers.dev/play/",
    "https://vavoo2.magnitude2.workers.dev/play/",
    "https://vavoo3.magnitude3.workers.dev/play/",
    "https://vavoo4.ogrenci.workers.dev/play/"
]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "iptv123")

security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    correct_pass = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Yetkisiz erişim",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# ─────────────────────────────────────────────
# Kanal Yükleme ve GitHub Senkronizasyonu
# ─────────────────────────────────────────────

def _load_channels_uncached():
    local_channels = {}
    github_channels = {}

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                local_channels = json.load(f)
                if not isinstance(local_channels, dict):
                    local_channels = {}
        except Exception:
            pass

    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            import requests
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CONFIG_FILE}"
            headers = {
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            }
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                fd = res.json()
                if "content" in fd:
                    dc = base64.b64decode(fd["content"]).decode("utf-8")
                    github_channels = json.loads(dc)
                    if not isinstance(github_channels, dict):
                        github_channels = {}
        except Exception as e:
            print(f"GitHub Load Error: {e}")

    merged = {**local_channels, **github_channels}
    return merged

def load_channels(force=False):
    now = time.time()
    if not force and _CHANNELS_CACHE["data"] is not None \
       and now - _CHANNELS_CACHE["time"] < CHANNELS_CACHE_TTL:
        return _CHANNELS_CACHE["data"]

    merged = _load_channels_uncached()
    _CHANNELS_CACHE["data"] = merged
    _CHANNELS_CACHE["time"] = now
    return merged

def save_and_push_channels(channels):
    _CHANNELS_CACHE["data"] = channels
    _CHANNELS_CACHE["time"] = time.time()

    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            import requests
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CONFIG_FILE}"
            headers = {
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            }
            res = requests.get(url, headers=headers, timeout=10)
            sha = res.json().get("sha") if res.status_code == 200 else None

            cs = json.dumps(channels, ensure_ascii=False, indent=4)
            ce = base64.b64encode(cs.encode("utf-8")).decode("utf-8")
            data = {
                "message": "Vercel admin panelinden channels1.json güncellendi",
                "content": ce,
            }
            if sha:
                data["sha"] = sha

            requests.put(url, headers=headers, json=data, timeout=15)
        except Exception as e:
            print(f"GitHub Sync Error: {e}")

# ─────────────────────────────────────────────
# Endpoint'ler
# ─────────────────────────────────────────────

@app.get("/ping")
def ping():
    return {"status": "OK"}

@app.get("/play/{channel_name}")
async def handle_stream(channel_name: str):
    channel_name = channel_name.strip().lower()
    channels = load_channels()
    if channel_name not in channels:
        return JSONResponse({"error": "Böyle bir kanal tanımlı değil."}, status_code=404)

    # Hızlı ve hafif: Limitli worker'ları elemek için arka planda hızlı test yapıp çalışan birini seçer,
    # aksi takdirde rastgele dağıtır.
    async with httpx.AsyncClient() as client:
        tasks = []
        for worker in WORKER_SERVERS:
            test_url = f"{worker}{channel_name}"
            tasks.append(client.head(test_url, timeout=1.0, follow_redirects=False))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)

    working_workers = []
    for i, res in enumerate(results):
        if not isinstance(res, Exception) and res.status_code in [200, 302, 307]:
            working_workers.append(WORKER_SERVERS[i])

    # Eğer çalışan worker bulunduysa en hızlısını seç, yoksa rastgele birine git
    chosen_worker = random.choice(working_workers) if working_workers else random.choice(WORKER_SERVERS)
    target_url = f"{chosen_worker}{channel_name}"

    return RedirectResponse(url=target_url, status_code=302)

@app.get("/playlist.m3u")
def generate_m3u(request: Request):
    channels = load_channels()
    m3u_content = "#EXTM3U\n"
    base_url = str(request.base_url).rstrip("/")
    
    for name in channels.keys():
        m3u_content += f'#EXTINF:-1 tvg-name="{name}" group-title="Vavoo Worker Proxy",{name.upper()}\n'
        m3u_content += f"{base_url}/play/{name}\n"
        
    headers = {
        "Cache-Control": "public, max-age=300, s-maxage=300"
    }
    return HTMLResponse(content=m3u_content, media_type="audio/x-mpegurl", headers=headers)

@app.get("/")
def read_root():
    return HTMLResponse("<h1>Multi-Worker IPTV Proxy Aktif</h1><p>Yönetim paneli: <a href='/admin'>/admin</a> | M3U Liste: <a href='/playlist.m3u'>/playlist.m3u</a></p>")

@app.get("/admin", response_class=HTMLResponse)
async def dashboard(user: str = Depends(verify_admin)):
    channels = load_channels()
    channels_json_esc = json.dumps(channels, ensure_ascii=False)

    html = f"""
    <!DOCTYPE html>
    <html lang="tr">
    <head>
        <meta charset="UTF-8">
        <title>Multi-Worker IPTV Admin Paneli</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
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
            .btn-secondary {{ background: #475569; padding: 6px 12px; font-size: 12px; margin-left: 10px; display: none; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; vertical-align: top; }}
            th {{ background: #0f172a; color: #38bdf8; font-weight: 600; }}
            tr:last-child td {{ border-bottom: none; }}
            a {{ color: #38bdf8; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .stats-bar {{ display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 15px; align-items: center; }}
            .stat-item {{ background: #1e293b; padding: 10px 15px; border-radius: 6px; font-size: 13px; }}
            .stat-item strong {{ color: #38bdf8; font-size: 16px; display: block; }}
        </style>
        <script>
            const allChannels = {channels_json_esc};

            function editChannel(name) {{
                const urls = allChannels[name];
                if (urls) {{
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
                        const t = td.textContent || td.innerText;
                        trs[i].style.display = (t.toLowerCase().indexOf(input) > -1) ? "" : "none";
                    }}
                }}
            }}
        </script>
    </head>
    <body>
        <div class="container">
            <h1>Multi-Worker IPTV Admin Paneli</h1>
            <p class="subtitle">Cloudflare Worker & channels1.json Senkronize Mimarisi Aktif</p>

            <div class="stats-bar">
                <div class="stat-item"><strong>{len(channels)}</strong>Tanımlı Kanal</div>
                <div class="stat-item"><a href="/playlist.m3u" target="_blank">📥 M3U Çalma Listesini İndir</a></div>
            </div>

            <div class="card">
                <h3 id="form-title">Yeni Kanal Ekle</h3>
                <form action="/admin/save" method="POST">
                    <input type="hidden" id="old_name" name="old_name" value="">
                    <label>Kanal Adı (Örn: sportv):</label>
                    <input type="text" id="channel_name" name="channel_name" required placeholder="sportv">
                    <label>M3U8 Linkleri (Alt alta veya virgülle ayırarak):</label>
                    <textarea id="urls" name="urls" rows="3" required placeholder="https://ornek.com/kanal1.m3u8"></textarea>
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
                            <th>Oynatıcı Linki</th>
                            <th>İşlem</th>
                        </tr>
                    </thead>
                    <tbody>
    """

    for name, sources in channels.items():
        safe_name_html = escape(name)
        safe_name_js = json.dumps(name)
        play_link = f"/play/{name}"
        delete_link = f"/admin/delete/{name}"

        html += f"""
                        <tr>
                            <td><strong>{safe_name_html}</strong><br><small style="color:#94a3b8;">{len(sources)} kaynak tanımlı</small></td>
                            <td>
                                <a href="{escape(play_link)}" target="_blank">{escape(play_link)}</a>
                            </td>
                            <td>
                                <button type="button" class="btn-warning" onclick='editChannel({safe_name_js})'>Düzenle</button>
                                <a href="{escape(delete_link)}" class="btn-danger" style="color:white; padding: 6px 10px; border-radius:4px; display:inline-block;" onclick="return confirm('Bu kanalı silmek istediğine emin misin?')">Sil</a>
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
    return HTMLResponse(content=html)

@app.post("/admin/save")
async def admin_save(old_name: str = Form(""), channel_name: str = Form(...), urls: str = Form(...), user: str = Depends(verify_admin)):
    old_name = old_name.strip().lower()
    channel_name = channel_name.strip().lower()
    parsed_urls = [u.strip() for u in urls.replace(',', '\n').split('\n') if u.strip()]

    if channel_name and parsed_urls:
        channels = load_channels()
        if old_name and old_name != channel_name and old_name in channels:
            del channels[old_name]

        channels[channel_name] = parsed_urls
        await asyncio.to_thread(save_and_push_channels, channels)

    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/delete/{channel_name}")
async def admin_delete(channel_name: str, user: str = Depends(verify_admin)):
    channel_name = channel_name.strip().lower()
    channels = load_channels()
    if channel_name in channels:
        del channels[channel_name]
        await asyncio.to_thread(save_and_push_channels, channels)

    return RedirectResponse(url="/admin", status_code=303)
