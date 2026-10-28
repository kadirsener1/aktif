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
            .card {{ background: #1e293b; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 4px 6px -
