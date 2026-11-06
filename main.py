import aiohttp
from aiohttp import web
import asyncio
import json
import os
import re
import time
import base64
from html import escape

CONFIG_FILE = "channels.json"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "iptv123")


# ─────────────────────────────────────────────
# Kanal yükleme / kaydetme (Cache yok, anlık okunur)
# ─────────────────────────────────────────────

def load_channels():
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

    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

    return merged


def save_and_push_channels(channels):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(channels, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Local Save Error: {e}")

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
                "message": "Admin panelinden channels.json güncellendi ve senkronize edildi",
                "content": ce,
            }
            if sha:
                data["sha"] = sha

            put_res = requests.put(url, headers=headers, json=data, timeout=15)
            if put_res.status_code not in (200, 201):
                print(f"GitHub Push Error: {put_res.status_code} {put_res.text[:200]}")
        except Exception as e:
            print(f"GitHub Sync Error: {e}")


# ─────────────────────────────────────────────
# Yetkilendirme
# ─────────────────────────────────────────────

def check_auth(request):
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return False
    try:
        auth_type, encoded = auth_header.split(None, 1)
        if auth_type.lower() == 'basic':
            decoded = base64.b64decode(encoded).decode('utf-8')
            u, p = decoded.split(':', 1)
            return u == ADMIN_USER and p == ADMIN_PASS
    except Exception:
        pass
    return False


def basic_auth_required(handler):
    async def wrapper(request):
        if not check_auth(request):
            return web.Response(
                status=401,
                headers={'WWW-Authenticate': 'Basic realm="IPTV Admin Paneli"'},
                text="Yetkisiz erişim. Lütfen giriş yapın.",
            )
        return await handler(request)
    return wrapper


# ─────────────────────────────────────────────
# M3U8 çekme
# ─────────────────────────────────────────────

async def fetch_m3u8(session, url, timeout_sec=5.0):
    start = time.time()
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with session.get(url, timeout=timeout) as response:
            elapsed = time.time() - start
            if response.status != 200:
                return None, response.status, elapsed
            content = await response.text()
            return content, 200, elapsed
    except Exception:
        elapsed = time.time() - start
        raise


# ─────────────────────────────────────────────
# Yapı sağlığı kontrolü
# ─────────────────────────────────────────────

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
    if has_target_duration:
        health += 20
    else:
        details["reasons"].append("TARGETDURATION eksik")

    if segment_count >= 3:
        health += 20
        if segment_count >= 6:
            health += 10
    else:
        details["reasons"].append(f"Segment sayısı az ({segment_count})")

    if consistent:
        health += 20
    elif segment_count >= 3:
        details["reasons"].append("Segment süreleri tutarsız")

    if details["has_media_sequence"]:
        health += 10

    if is_master:
        health += 10

    if details["is_live"]:
        health += 10

    health = min(health, 100.0)
    is_playable = is_master or segment_count > 0

    return health, details, is_playable


# ─────────────────────────────────────────────
# URL testi ve Sıralı (Failover) Kontrolü
# ─────────────────────────────────────────────

async def check_url(session, url):
    try:
        content, status, elapsed_time = await fetch_m3u8(session, url, 4.0)

        if status != 200:
            return {
                "url": url, "ok": False, "reason": f"HTTP {status}",
                "score": 0, "speed": elapsed_time,
            }

        if not content:
            return {
                "url": url, "ok": False, "reason": "Boş yanıt",
                "score": 0, "speed": elapsed_time,
            }

        health_score, details, is_playable = analyze_manifest_health(content)

        if not is_playable or health_score < 40:
            return {
                "url": url, "ok": False,
                "reason": "Oynatılamaz veya zayıf manifest",
                "score": 0, "speed": elapsed_time,
                "health": health_score,
                "health_details": details,
            }

        speed_score = max(0.0, 100.0 - elapsed_time * 28.0)
        final_score = speed_score * 0.60 + health_score * 0.40

        return {
            "url": url, "ok": True, "reason": "OK",
            "score": final_score,
            "speed": elapsed_time,
            "speed_score": speed_score,
            "health": health_score,
            "health_details": details,
        }
    except asyncio.TimeoutError:
        return {
            "url": url, "ok": False, "reason": "Timeout",
            "score": 0, "speed": 4.0,
        }
    except Exception as e:
        return {
            "url": url, "ok": False, "reason": f"Hata: {str(e)[:40]}",
            "score": 0, "speed": 0,
        }


async def test_all_sources(channel_name):
    channels = load_channels()
    sources = channels.get(channel_name)
    if not sources:
        return []

    async with aiohttp.ClientSession() as session:
        tasks = [check_url(session, u) for u in sources]
        results = await asyncio.gather(*tasks)

    return [r for r in results if r is not None]


async def get_working_stream(channel_name):
    """
    Önbellek kullanılmaz. Tanımlanan sunucular sırasıyla (liste sırasına göre)
    test edilir. Hangi sunucu yanıt verirse anında o döndürülür. 
    Böylece Cloudflare limitine takılan sunucu atlanıp sonrakine geçilir.
    """
    channels = load_channels()
    sources = channels.get(channel_name)
    if not sources:
        return None

    async with aiohttp.ClientSession() as session:
        for url in sources:
            res = await check_url(session, url)
            if res and res["ok"]:
                return res["url"]
                
    return None


# ─────────────────────────────────────────────
# HTTP handler'ları
# ─────────────────────────────────────────────

async def ping(request):
    return web.Response(text="OK", status=200)


async def handle_stream(request):
    channel_name = request.match_info.get('channel_name', '').strip().lower()
    channels = load_channels()
    if channel_name not in channels:
        return web.Response(text="Böyle bir kanal tanımlı değil.", status=404)

    working_url = await get_working_stream(channel_name)
    if not working_url:
        return web.Response(
            text=f"{channel_name} için şu an aktif çalışan sunucu bulunamadı.",
            status=503,
        )
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

        channels[channel_name] = urls

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, save_and_push_channels, channels)

    raise web.HTTPFound('/')


@basic_auth_required
async def admin_delete(request):
    channel_name = request.match_info.get('channel_name', '').strip().lower()
    channels = load_channels()
    if channel_name in channels:
        del channels[channel_name]

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, save_and_push_channels, channels)

    raise web.HTTPFound('/')


@basic_auth_required
async def admin_clear_cache(request):
    raise web.HTTPFound('/')


@basic_auth_required
async def admin_test_channel(request):
    channel_name = request.match_info.get('channel_name', '').strip().lower()
    channels = load_channels()
    if channel_name not in channels:
        return web.json_response({"error": "Kanal bulunamadı"}, status=404)

    results = await test_all_sources(channel_name)
    best_url = None
    working = [r for r in results if r["ok"]]
    if working:
        working.sort(key=lambda x: x["score"], reverse=True)
        best_url = working[0]["url"]

    return web.json_response({
        "channel": channel_name,
        "best_url": best_url,
        "results": results,
    })


@basic_auth_required
async def dashboard(request):
    channels = load_channels()
    channels_json_esc = json.dumps(channels, ensure_ascii=False)

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
            .btn-secondary {{ background: #475569; padding: 6px 12px; font-size: 12px; margin-left: 10px; display: none; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; vertical-align: top; }}
            th {{ background: #0f172a; color: #38bdf8; font-weight: 600; }}
            tr:last-child td {{ border-bottom: none; }}
            .badge-active {{ background: #1e3a8a; color: #60a5fa; padding: 4px 8px; border-radius: 4px; font-size: 12px; }}
            a {{ color: #38bdf8; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .score-detail {{ display: none; background: #0f172a; padding: 12px; border-radius: 6px; margin-top: 8px; }}
            .score-detail.open {{ display: block; }}
            .score-row {{ padding: 8px; margin: 4px 0; background: #1e293b; border-radius: 4px; border-left: 3px solid #475569; }}
            .score-row.best {{ border-left-color: #22c55e; background: #052e16; }}
            .score-row.failed {{ border-left-color: #dc2626; background: #450a0a; opacity: 0.7; }}
            .score-url {{ font-family: monospace; font-size: 11px; color: #cbd5e1; word-break: break-all; }}
            .score-badge {{ font-weight: bold; padding: 3px 8px; border-radius: 4px; font-size: 11px; }}
            .score-fail {{ background: #dc2626; color: #ffffff; }}
            .loading {{ color: #94a3b8; font-style: italic; padding: 10px; }}
            .stats-bar {{ display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 15px; }}
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

                    const sorted = data.results.slice().sort((a, b) => b.score - a.score);
                    let html = '<div style="margin-bottom:8px; color:#94a3b8; font-size:12px;">📊 ' + sorted.length + ' kaynak test edildi.</div>';

                    sorted.forEach((r, idx) => {{
                        const isBest = r.url === data.best_url;
                        const cssClass = !r.ok ? 'failed' : (isBest ? 'best' : '');
                        html += '<div class="score-row ' + cssClass + '">';
                        html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; flex-wrap:wrap; gap:6px;">';
                        html += '<strong>#' + (idx+1) + ' ';
                        if (isBest) html += '🏆 ÖNERİLEN ';
                        if (!r.ok) html += '❌ ';
                        html += 'Skor: ' + r.score.toFixed(1) + '</strong>';
                        if (!r.ok) {{
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
            <h1>IPTV Failover Paneli</h1>
            <p class="subtitle">Cloudflare limitlerine karşı sıralı failover sistemi aktif.</p>

            <div class="stats-bar">
                <div class="stat-item"><strong>{len(channels)}</strong>Tanımlı Kanal</div>
            </div>

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
                            <th>Oynatıcı Linki & Kaynaklar</th>
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
        sources_summary = f"{len(sources)} adet sunucu sırayla denenecek"

        html += f"""
                        <tr>
                            <td><strong>{safe_name_html}</strong></td>
                            <td><span class="badge-active">Aktif (Sıralı Failover)</span></td>
                            <td>
                                <a href="{escape(play_link)}" target="_blank">{escape(play_link)}</a><br>
                                <small style="color: #94a3b8; word-break: break-all;">{sources_summary}</small>
                                <div id="scores-{safe_name_html}" class="score-detail"></div>
                            </td>
                            <td>
                                <button class="btn-test" onclick='toggleScores({safe_name_js})'>📊 Test Et</button>
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
    return web.Response(text=html, content_type='text/html')


# ─────────────────────────────────────────────
# Uygulama Başlatma ve Rotalar
# ─────────────────────────────────────────────

app = web.Application()
app.router.add_get('/ping', ping)
app.router.add_get('/play/{channel_name}', handle_stream)
app.router.add_get('/', dashboard)
app.router.add_post('/admin/save', admin_save)
app.router.add_get('/admin/delete/{channel_name}', admin_delete)
app.router.add_get('/admin/clear-cache', admin_clear_cache)
app.router.add_get('/admin/test/{channel_name}', admin_test_channel)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)
