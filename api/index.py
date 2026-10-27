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

@app.get("/", response_class=HTMLResponse)
async def dashboard(user: str = Depends(verify_admin)):
    channels = load_channels()
    current_time = time.time()
    channels_json_esc = json.dumps(channels, ensure_ascii=False)
    stream_cache_count = len(STREAM_CACHE)
    m3u8_cache_count = len(M3U8_CACHE)

    # (Aynı HTML tasarımı burada yer alır - kod kalabalığı olmaması için özet dashboard şablonu)
    html = f"""
    <!DOCTYPE html>
    <html lang="tr">
    <head><meta charset="UTF-8"><title>IPTV Vercel Admin</title>
    <style>body{{font-family:sans-serif;background:#0f172a;color:#fff;padding:20px;}} .card{{background:#1e293b;padding:20px;border-radius:8px;margin-bottom:20px;}} input,textarea{{width:100%;padding:8px;margin:5px 0;background:#0f172a;color:#fff;border:1px solid #334155;}} button{{background:#0284c7;color:#fff;border:none;padding:8px 15px;cursor:pointer;border-radius:4px;}}</style>
    </head>
    <body>
    <div style="max-width:1000px;margin:0 auto;">
      <h1>IPTV Vercel Proxy Paneli</h1>
      <div class="card">
        <h3>Yeni Kanal Ekle</h3>
        <form action="/admin/save" method="POST">
          <input type="hidden" id="old_name" name="old_name" value="">
          <label>Kanal Adı:</label><input type="text" id="channel_name" name="channel_name" required>
          <label>M3U8 Linkleri:</label><textarea id="urls" name="urls" rows="3" required></textarea>
          <button type="submit" id="submit-btn">Kaydet ve GitHub'a Eşitle</button>
        </form>
      </div>
      <div class="card">
        <h3>Kanallar ({len(channels)})</h3>
        <ul>
    """
    for name, sources in channels.items():
        html += f"<li><b>{escape(name)}</b> - <a href='/play/{name}' target='_blank'>/play/{name}</a> (<a href='/admin/delete/{name}'>Sil</a>)</li>"
    html += "</ul></div></div></body></html>"
    return html

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

    return RedirectResponse(url="/", status_code=303)

@app.get("/admin/delete/{channel_name}")
async def admin_delete(channel_name: str, user: str = Depends(verify_admin)):
    channel_name = channel_name.strip().lower()
    channels = load_channels()
    if channel_name in channels:
        del channels[channel_name]
        STREAM_CACHE.pop(channel_name, None)
        SCORE_CACHE.pop(channel_name, None)
        await asyncio.to_thread(save_and_push_channels, channels)

    return RedirectResponse(url="/", status_code=303)

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
