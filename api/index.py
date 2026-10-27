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

app = FastAPI()

CONFIG_FILE = "channels.json"
STREAM_CACHE = {}
SCORE_CACHE = {}
M3U8_CACHE = {}
CACHE_DURATION = 60
M3U8_CACHE_DURATION = 300
MAX_M3U8_CACHE = 300

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
# Kanal yükleme / kaydetme
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
# M3U8 çekme ve Sağlık Analizi
# ─────────────────────────────────────────────

async def fetch_m3u8(client, url, timeout_sec=5.0):
    now = time.time()
    if url in M3U8_CACHE:
        content, ts, cached_elapsed = M3U8_CACHE[url]
        if now - ts < M3U8_CACHE_DURATION:
            return content, 200, cached_elapsed

    start = time.time()
    try:
        response = await client.get(url, timeout=timeout_sec, follow_redirects=True)
        elapsed = time.time() - start
        if response.status_code != 200:
            return None, response.status_code, elapsed
        content = response.text
    except Exception:
        elapsed = time.time() - start
        raise

    if len(M3U8_CACHE) >= MAX_M3U8_CACHE:
        oldest = sorted(M3U8_CACHE.items(), key=lambda x: x[1][1])[:80]
        for k, _ in oldest:
            M3U8_CACHE.pop(k, None)

    M3U8_CACHE[url] = (content, now, elapsed)
    return content, 200, elapsed

def analyze_manifest_health(content):
    details = {
        "has_target_duration": False,
        "segment_count": 0,
        "segment_durations_consistent": False,
        "has_media_sequence": False,
        "is_master": False,
        "is_live": False,
        "reasons": [],
    }

    if "#EXTM3U" not in content:
        details["reasons"].append("EXTM3U başlığı yok")
        return 0.0, details, False

    has_target_duration = "#EXT-X-TARGETDURATION" in content
    details["has_target_duration"] = has_target_duration
    is_master = "#EXT-X-STREAM-INF" in content
    details["is_master"] = is_master

    durations = re.findall(r'#EXTINF:\s*([\d.]+)', content)
    try:
        durations = [float(d) for d in durations]
    except ValueError:
        durations = []

    segment_count = len(durations)
    details["segment_count"] = segment_count

    consistent = False
    if len(durations) >= 3:
        avg = sum(durations) / len(durations)
        if avg > 0:
            variance = sum((d - avg) ** 2 for d in durations) / len(durations)
            std_dev = variance ** 0.5
            if std_dev / avg < 0.30:
                consistent = True
    details["segment_durations_consistent"] = consistent
    details["has_media_sequence"] = "#EXT-X-MEDIA-SEQUENCE" in content
    details["is_live"] = "#EXT-X-ENDLIST" not in content

    health = 0.0
    if has_target_duration: health += 20
    if segment_count >= 3:
        health += 20
        if segment_count >= 6: health += 10
    if consistent: health += 20
    elif segment_count >= 3: details["reasons"].append("Segment süreleri tutarsız")

    if details["has_media_sequence"]: health += 10
    if is_master: health += 10
    if details["is_live"]: health += 10

    health = min(health, 100.0)
    is_playable = is_master or segment_count > 0
    return health, details, is_playable

async def check_url(client, url):
    try:
        content, status, elapsed_time = await fetch_m3u8(client, url, 5.0)
        if status != 200 or not content:
            return {"url": url, "ok": False, "reason": f"HTTP {status}" if status != 200 else "Boş yanıt", "score": 0, "speed": elapsed_time}

        health_score, details, is_playable = analyze_manifest_health(content)
        if not is_playable or health_score < 40:
            return {"url": url, "ok": False, "reason": "Oynatılamaz veya sağlık düşük", "score": 0, "speed": elapsed_time, "health": health_score}

        speed_score = max(0.0, 100.0 - elapsed_time * 28.0)
        final_score = speed_score * 0.60 + health_score * 0.40
        return {"url": url, "ok": True, "reason": "OK", "score": final_score, "speed": elapsed_time, "speed_score": speed_score, "health": health_score, "health_details": details}
    except Exception as e:
        return {"url": url, "ok": False, "reason": f"Hata", "score": 0, "speed": 0}

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

    working = [r for r in results if r["ok"]]
    if working:
        working.sort(key=lambda x: x["score"], reverse=True)
        STREAM_CACHE[channel_name] = {"url": working[0]["url"], "time": current_time, "score": working[0]["score"]}
    else:
        STREAM_CACHE.pop(channel_name, None)

    return results

async def get_working_stream(channel_name):
    current_time = time.time()
    if channel_name in STREAM_CACHE:
        c = STREAM_CACHE[channel_name]
        if current_time - c["time"] < CACHE_DURATION:
            return c["url"]

    results = await test_all_sources(channel_name, force=True)
    working = [r for r in results if r["ok"]]
    if not working: return None
    working.sort(key=lambda x: x["score"], reverse=True)
    return working[0]["url"]

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

    working_url = await get_working_stream(channel_name)
    if not working_url:
        return JSONResponse({"error": f"{channel_name} için aktif yayın bulunamadı."}, status_code=503)
    
    return RedirectResponse(url=working_url, status_code=302)

@app.get("/")
def read_root():
    return HTMLResponse("<h1>IPTV Proxy Sistem Aktif</h1><p>Yönetim paneline gitmek için <a href='/admin'>/admin</a> adresini kullanın.</p>")

@app.get("/admin", response_class=HTMLResponse)
async def dashboard(user: str = Depends(verify_admin)):
    channels = load_channels()
    current_time = time.time()
    channels_json_esc = json.dumps(channels, ensure_ascii=False)
    stream_cache_count = len(STREAM_CACHE)
    m3u8_cache_count = len(M3U8_CACHE)

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
            .btn-clear {{ background: #0d9488; margin-bottom: 15px; }}
            .btn-clear:hover {{ background: #0f766e; }}
            .btn-secondary {{ background: #475569; padding: 6px 12px; font-size: 12px; margin-left: 10px; display: none; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; vertical-align: top; }}
            th {{ background: #0f172a; color: #38bdf8; font-weight: 600; }}
            tr:last-child td {{ border-bottom: none; }}
            .badge-cached {{ background: #1e3a8a; color: #60a5fa; padding: 4px 8px; border-radius: 4px; font-size: 12px; }}
            .badge-inactive {{ background: #334155; color: #94a3b8; padding: 4px 8px; border-radius: 4px; font-size: 12px; }}
            a {{ color: #38bdf8; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .score-detail {{ display: none; background: #0f172a; padding: 12px; border-radius: 6px; margin-top: 8px; }}
            .score-detail.open {{ display: block; }}
            .score-row {{ padding: 8px; margin: 4px 0; background: #1e293b; border-radius: 4px; border-left: 3px solid #475569; }}
            .score-row.best {{ border-left-color: #22c55e; background: #052e16; }}
            .score-row.failed {{ border-left-color: #dc2626; background: #450a0a; opacity: 0.7; }}
            .score-url {{ font-family: monospace; font-size: 11px; color: #cbd5e1; word-break: break-all; }}
            .score-stats {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 6px; font-size: 11px; }}
            .stat {{ background: #334155; padding: 3px 8px; border-radius: 4px; color: #e2e8f0; }}
            .stat.good {{ background: #166534; color: #bbf7d0; }}
            .stat.mid {{ background: #854d0e; color: #fef08a; }}
            .stat.bad {{ background: #991b1b; color: #fecaca; }}
            .score-badge {{ font-weight: bold; padding: 3px 8px; border-radius: 4px; font-size: 11px; }}
            .score-high {{ background: #22c55e; color: #052e16; }}
            .score-mid {{ background: #f59e0b; color: #431407; }}
            .score-low {{ background: #6b7280; color: #0f172a; }}
            .score-fail {{ background: #dc2626; color: #ffffff; }}
            .loading {{ color: #94a3b8; font-style: italic; padding: 10px; }}
            .stats-bar {{ display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 15px; }}
            .stat-item {{ background: #1e293b; padding: 10px 15px; border-radius: 6px; font-size: 13px; }}
            .stat-item strong {{ color: #38bdf8; font-size: 16px; display: block; }}
            .health-bar {{ display: inline-block; width: 60px; height: 6px; background: #334155; border-radius: 3px; overflow: hidden; vertical-align: middle; margin-left: 4px; }}
            .health-bar-fill {{ height: 100%; }}
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

            function scoreBadge(score) {{
                if (score >= 75) return "score-high";
                if (score >= 55) return "score-mid";
                return "score-low";
            }}

            function scoreLabel(score) {{
                if (score >= 75) return "ÇOK İYİ ✅";
                if (score >= 55) return "İYİ 👍";
                if (score >= 35) return "ORTA ⚠️";
                return "ZAYIF ⚠️";
            }}

            function statClass(value, good, mid) {{
                if (value >= good) return "good";
                if (value >= mid) return "mid";
                return "bad";
            }}

            function healthBar(score) {{
                let color = "#6b7280";
                if (score >= 75) color = "#22c55e";
                else if (score >= 55) color = "#f59e0b";
                else if (score >= 40) color = "#ef4444";
                return '<span class="health-bar"><span class="health-bar-fill" style="width:' + score + '%; background:' + color + ';"></span></span>';
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

                    const sorted = data.results.slice().sort((a, b) => b.score - a.score);
                    let html = '<div style="margin-bottom:8px; color:#94a3b8; font-size:12px;">📊 ' + sorted.length + ' kaynak test edildi. Skor = Hız (%60) + Yapı Sağlığı (%40).</div>';

                    sorted.forEach((r, idx) => {{
                        const isBest = r.url === data.best_url;
                        const cssClass = !r.ok ? 'failed' : (isBest ? 'best' : '');
                        html += '<div class="score-row ' + cssClass + '">';
                        html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; flex-wrap:wrap; gap:6px;">';
                        html += '<strong>#' + (idx+1) + ' ';
                        if (isBest) html += '🏆 SEÇİLDİ ';
                        if (!r.ok) html += '❌ ';
                        html += 'Skor: ' + r.score.toFixed(1) + '</strong>';
                        if (r.ok) {{
                            html += '<span class="score-badge ' + scoreBadge(r.score) + '">' + scoreLabel(r.score) + '</span>';
                        }} else {{
                            html += '<span class="score-badge score-fail">' + r.reason + '</span>';
                        }}
                        html += '</div>';
                        html += '<div class="score-url">' + r.url + '</div>';
                        if (r.ok) {{
                            html += '<div class="score-stats">';
                            html += '<span class="stat ' + statClass(r.speed_score, 70, 50) + '">Hız: ' + (r.speed*1000).toFixed(0) + ' ms</span>';
                            html += '<span class="stat ' + statClass(r.health, 75, 55) + '">Yapı: ' + r.health.toFixed(0) + healthBar(r.health) + '</span>';
                            html += '</div>';
                        }}
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
            <p class="subtitle">Vercel Serverless Modunda Çalışıyor</p>

            <div class="stats-bar">
                <div class="stat-item"><strong>{len(channels)}</strong>Tanımlı Kanal</div>
                <div class="stat-item"><strong>{stream_cache_count}</strong>Aktif Yayın</div>
                <div class="stat-item"><strong>{m3u8_cache_count}</strong>M3U8 Cache</div>
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
                            <th>Durum</th>
                            <th>Oynatıcı Linki & Seçilen Kaynak</th>
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

        cache_info = STREAM_CACHE.get(name)
        if cache_info and (current_time - cache_info["time"] < CACHE_DURATION):
            score_text = f" (Skor: {cache_info.get('score', 0):.1f})"
            status_html = '<span class="badge-cached">Önbellekte Aktif</span>'
            active_url = escape(cache_info["url"] + score_text)
        else:
            status_html = '<span class="badge-inactive">Test Bekliyor</span>'
            active_url = f"{len(sources)} kaynak tanımlı"

        html += f"""
                        <tr>
                            <td><strong>{safe_name_html}</strong></td>
                            <td>{status_html}</td>
                            <td>
                                <a href="{escape(play_link)}" target="_blank">{escape(play_link)}</a><br>
                                <small style="color: #94a3b8; word-break: break-all;">{active_url}</small>
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
            STREAM_CACHE.pop(old_name, None)
            SCORE_CACHE.pop(old_name, None)

        channels[channel_name] = parsed_urls
        STREAM_CACHE.pop(channel_name, None)
        SCORE_CACHE.pop(channel_name, None)

        await asyncio.to_thread(save_and_push_channels, channels)

    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/delete/{channel_name}")
async def admin_delete(channel_name: str, user: str = Depends(verify_admin)):
    channel_name = channel_name.strip().lower()
    channels = load_channels()
    if channel_name in channels:
        del channels[channel_name]
        STREAM_CACHE.pop(channel_name, None)
        SCORE_CACHE.pop(channel_name, None)
        await asyncio.to_thread(save_and_push_channels, channels)

    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/test/{channel_name}")
async def admin_test_channel(channel_name: str, user: str = Depends(verify_admin)):
    channel_name = channel_name.strip().lower()
    channels = load_channels()
    if channel_name not in channels:
    
        return JSONResponse({"error": "Kanal bulunamadı"}, status_code=404)

    results = await test_all_sources(channel_name, force=True)
    best_url = None
    working = [r for r in results if r["ok"]]
    if working:
        working.sort(key=lambda x: x["score"], reverse=True)
        best_url = working[0]["url"]

    return {"channel": channel_name, "best_url": best_url, "results": results}
