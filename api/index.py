from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import asyncio
import json
import os
import re
import time
import base64
from html import escape
import httpx
import secrets

# Vercel'in giriş noktasını (entrypoint) bulabilmesi için 'app' en üst seviyede tanımlanmıştır.
app = FastAPI()

CONFIG_FILE = "channels.json"
STREAM_CACHE = {}
SCORE_CACHE = {}
M3U8_CACHE = {}
CACHE_DURATION = 60
M3U8_CACHE_DURATION = 300
MAX_M3U8_CACHE = 300

CHANNEL_FAILOVER_STATE = {}
STREAM_BLACKLIST = {}
BLACKLIST_DURATION = 60  # 1 dakika

_CHANNELS_CACHE = {"data": None, "time": 0}
CHANNELS_CACHE_TTL = 30

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
                "message": "Vercel admin panelinden channels.json güncellendi",
                "content": ce,
            }
            if sha:
                data["sha"] = sha

            requests.put(url, headers=headers, json=data, timeout=15)
        except Exception as e:
            print(f"GitHub Sync Error: {e}")

# ─────────────────────────────────────────────
# M3U8 Doğrulama ve Sağlık Analizi
# ─────────────────────────────────────────────

async def fetch_m3u8(client, url, timeout_sec=5.0):
    start = time.time()
    try:
        response = await client.get(url, timeout=timeout_sec, follow_redirects=True)
        elapsed = time.time() - start
        if response.status_code != 200:
            return None, response.status_code, elapsed
        return response.text, 200, elapsed
    except Exception:
        elapsed = time.time() - start
        return None, 500, elapsed

async def check_url(client, url):
    try:
        content, status, elapsed_time = await fetch_m3u8(client, url, 5.0)
        if status != 200 or not content or "#EXTM3U" not in content:
            return {"url": url, "ok": False, "reason": f"HTTP {status}" if status != 200 else "Boş Yanıt", "score": 0}

        has_segments = "#EXTINF:" in content or "#EXT-X-STREAM-INF" in content
        if not has_segments:
            return {"url": url, "ok": False, "reason": "Segment Yok", "score": 0}

        speed_score = max(0.0, 100.0 - elapsed_time * 20.0)
        return {"url": url, "ok": True, "reason": "OK", "score": speed_score, "speed": elapsed_time}
    except Exception as e:
        return {"url": url, "ok": False, "reason": "Bağlantı Hatası", "score": 0}

async def test_all_sources(channel_name, force=False):
    current_time = time.time()
    if not force and channel_name in SCORE_CACHE:
        c = SCORE_CACHE[channel_name]
        if current_time - c["time"] < CACHE_DURATION:
            return c["results"]

    channels = load_channels()
    sources = channels.get(channel_name)
    if not sources:
        return []

    async with httpx.AsyncClient() as client:
        tasks = [check_url(client, u) for u in sources]
        results = await asyncio.gather(*tasks)

    results = [r for r in results if r is not None]
    SCORE_CACHE[channel_name] = {"time": current_time, "results": results}
    return results

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

    original_sources = channels.get(channel_name, [])

    # 1. Kara liste temizliği (Süresi dolanları çıkar)
    now = time.time()
    if channel_name in STREAM_BLACKLIST:
        STREAM_BLACKLIST[channel_name] = {
            url: t for url, t in STREAM_BLACKLIST[channel_name].items() 
            if now - t < BLACKLIST_DURATION
        }
    else:
        STREAM_BLACKLIST[channel_name] = {}

    blacklisted_urls = STREAM_BLACKLIST[channel_name]

    # 2. Kaynakları test et
    results = await test_all_sources(channel_name, force=True)
    working_dict = {r["url"]: r["score"] for r in results if r["ok"]}

    ordered_working_urls = [
        url for url in original_sources 
        if url in working_dict and url not in blacklisted_urls
    ]

    if not ordered_working_urls:
        STREAM_BLACKLIST[channel_name] = {}
        ordered_working_urls = [url for url in original_sources if url in working_dict]

    if not ordered_working_urls:
        return JSONResponse({"error": f"{channel_name} için aktif yayın bulunamadı."}, status_code=503)

    # 3. İndeks yönetimi ve Akıllı Donma Yakalama (Runtime Failover)
    state = CHANNEL_FAILOVER_STATE.get(channel_name, {"index": 0, "last_request": 0, "url": None})
    
    if state["url"] and (now - state["last_request"] < 30):
        STREAM_BLACKLIST[channel_name][state["url"]] = now
        current_index = (state["index"] + 1) % len(ordered_working_urls)
    else:
        current_index = state["index"]

    if current_index >= len(ordered_working_urls):
        current_index = 0

    target_url = ordered_working_urls[current_index]

    CHANNEL_FAILOVER_STATE[channel_name] = {
        "index": current_index,
        "last_request": now,
        "url": target_url
    }

    return RedirectResponse(url=target_url, status_code=302)

@app.get("/admin/reset-blacklist")
async def reset_blacklist(user: str = Depends(verify_admin)):
    STREAM_BLACKLIST.clear()
    CHANNEL_FAILOVER_STATE.clear()
    SCORE_CACHE.clear()
    M3U8_CACHE.clear()
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/playlist.m3u")
def generate_m3u(request: Request):
    channels = load_channels()
    m3u_content = "#EXTM3U\n"
    base_url = str(request.base_url).rstrip("/")
    
    for name in channels.keys():
        m3u_content += f'#EXTINF:-1 tvg-name="{name}" group-title="IPTV Proxy",{name.upper()}\n'
        m3u_content += f"{base_url}/play/{name}\n"
        
    return HTMLResponse(content=m3u_content, media_type="audio/x-mpegurl")

@app.get("/")
def read_root():
    return HTMLResponse("<h1>IPTV Proxy Sistem Aktif</h1><p>Yönetim paneline gitmek için <a href='/admin'>/admin</a> adresini kullanın. Çalma listesi için <a href='/playlist.m3u'>/playlist.m3u</a></p>")

@app.get("/admin", response_class=HTMLResponse)
async def dashboard(user: str = Depends(verify_admin)):
    channels = load_channels()
    channels_json_esc = json.dumps(channels, ensure_ascii=False)
    blacklisted_total = sum(len(v) for v in STREAM_BLACKLIST.values())

    html = f"""
    <!DOCTYPE html>
    <html lang="tr">
    <head>
        <meta charset="UTF-8">
        <title>IPTV Failover Admin Paneli</title>
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
            .btn-test {{ background: #7c3aed; padding: 6px 12px; font-size: 12px; margin-right: 5px; }}
            .btn-test:hover {{ background: #6d28d9; }}
            .btn-reset {{ background: #ea580c; padding: 10px 15px; font-size: 13px; font-weight: bold; border-radius: 6px; text-decoration: none; display: inline-block; color: white; }}
            .btn-reset:hover {{ background: #c2410c; }}
            .btn-secondary {{ background: #475569; padding: 6px 12px; font-size: 12px; margin-left: 10px; display: none; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; vertical-align: top; }}
            th {{ background: #0f172a; color: #38bdf8; font-weight: 600; }}
            tr:last-child td {{ border-bottom: none; }}
            a {{ color: #38bdf8; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .score-detail {{ display: none; background: #0f172a; padding: 12px; border-radius: 6px; margin-top: 8px; }}
            .score-detail.open {{ display: block; }}
            .score-row {{ padding: 8px; margin: 4px 0; background: #1e293b; border-radius: 4px; border-left: 3px solid #475569; }}
            .score-row.best {{ border-left-color: #22c55e; background: #052e16; }}
            .score-row.failed {{ border-left-color: #dc2626; background: #450a0a; opacity: 0.7; }}
            .score-url {{ font-family: monospace; font-size: 11px; color: #cbd5e1; word-break: break-all; }}
            .score-badge {{ font-weight: bold; padding: 3px 8px; border-radius: 4px; font-size: 11px; }}
            .score-high {{ background: #22c55e; color: #052e16; }}
            .score-fail {{ background: #dc2626; color: #ffffff; }}
            .loading {{ color: #94a3b8; font-style: italic; padding: 10px; }}
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

            async function testChannel(name) {{
                const container = document.getElementById('scores-' + name);
                container.classList.add('open');
                container.innerHTML = '<div class="loading">🔄 Linkler test ediliyor...</div>';

                try {{
                    const res = await fetch('/admin/test/' + encodeURIComponent(name));
                    const data = await res.json();

                    if (!data.results || data.results.length === 0) {{
                        container.innerHTML = '<div class="loading">⚠️ Sonuç yok.</div>';
                        return;
                    }}

                    let html = '<div style="margin-bottom:8px; color:#94a3b8; font-size:12px;">📊 ' + data.results.length + ' kaynak test edildi.</div>';

                    data.results.forEach((r, idx) => {{
                        const isBest = r.url === data.best_url;
                        const cssClass = !r.ok ? 'failed' : (isBest ? 'best' : '');
                        html += '<div class="score-row ' + cssClass + '">';
                        html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; flex-wrap:wrap; gap:6px;">';
                        html += '<strong>#' + (idx+1) + ' ';
                        if (isBest) html += '🏆 SEÇİLEN ';
                        if (!r.ok) html += '❌ ';
                        html += 'Skor: ' + r.score.toFixed(1) + '</strong>';
                        if (r.ok) {{
                            html += '<span class="score-badge score-high">ÇALIŞIYOR ✅</span>';
                        }} else {{
                            html += '<span class="score-badge score-fail">' + r.reason + '</span>';
                        }}
                        html += '</div>';
                        html += '<div class="score-url">' + r.url + '</div>';
                        html += '</div>';
                    }});

                    container.innerHTML = html;
                }} catch (e) {{
                    container.innerHTML = '<div class="loading">❌ Hata: ' + e.message + '</div>';
                }}
            }}

            function toggleScores(name) {{
                const container = document.getElementById('scores-' + name);
                if (container.classList.contains('open') && container.innerHTML.trim() !== '') {{
                    container.classList.remove('open');
                }} else {{
                    testChannel(name);
                }}
            }}
        </script>
    </head>
    <body>
        <div class="container">
            <h1>IPTV Smart Proxy Admin Paneli</h1>
            <p class="subtitle">Karalisteli & Donma Korumalı Akıllı Failover Modu Aktif</p>

            <div class="stats-bar">
                <div class="stat-item"><strong>{len(channels)}</strong>Tanımlı Kanal</div>
                <div class="stat-item"><strong>{blacklisted_total}</strong>Karalistede Yayın</div>
                <div class="stat-item"><a href="/playlist.m3u" target="_blank">📥 M3U Çalma Listesini İndir</a></div>
                <a href="/admin/reset-blacklist" class="btn-reset" onclick="return confirm('Tüm karalisteyi ve akış önbelleğini sıfırlamak istediğinize emin misiniz?')">🗑️ Karalisteyi ve Önbelleği Temizle</a>
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
                                <div id="scores-{safe_name_html}" class="score-detail"></div>
                            </td>
                            <td>
                                <button class="btn-test" onclick='toggleScores({safe_name_js})'>📊 Skorlar</button>
                                <button class="btn-warning" onclick='editChannel({safe_name_js})'>Düzenle</button>
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
            SCORE_CACHE.pop(old_name, None)
            CHANNEL_FAILOVER_STATE.pop(old_name, None)
            STREAM_BLACKLIST.pop(old_name, None)

        channels[channel_name] = parsed_urls
        SCORE_CACHE.pop(old_name, None)
        CHANNEL_FAILOVER_STATE.pop(old_name, None)
        STREAM_BLACKLIST.pop(old_name, None)

        await asyncio.to_thread(save_and_push_channels, channels)

    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/delete/{channel_name}")
async def admin_delete(channel_name: str, user: str = Depends(verify_admin)):
    channel_name = channel_name.strip().lower()
