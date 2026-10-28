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
@@ -192,39 +191,43 @@

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
@@ -242,7 +245,24 @@

    return RedirectResponse(url=target_url, status_code=302)

# 🟢 YENİ: Karalisteyi ve durum hafızasını sıfırlayan endpoint

















@app.get("/admin/reset-blacklist")
async def reset_blacklist(user: str = Depends(verify_admin)):
    STREAM_BLACKLIST.clear()
@@ -271,7 +291,6 @@
async def dashboard(user: str = Depends(verify_admin)):
    channels = load_channels()
    channels_json_esc = json.dumps(channels, ensure_ascii=False)
    m3u8_cache_count = len(M3U8_CACHE)
    blacklisted_total = sum(len(v) for v in STREAM_BLACKLIST.values())

    html = f"""
@@ -317,6 +336,8 @@
            .stats-bar {{ display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 15px; align-items: center; }}
            .stat-item {{ background: #1e293b; padding: 10px 15px; border-radius: 6px; font-size: 13px; }}
            .stat-item strong {{ color: #38bdf8; font-size: 16px; display: block; }}


        </style>
        <script>
            const allChannels = {channels_json_esc};
@@ -410,13 +431,13 @@
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
@@ -439,7 +460,7 @@
                    <thead>
                        <tr>
                            <th>Kanal Adı</th>
                            <th>Oynatıcı Linki</th>
                            <th>İşlem</th>
                        </tr>
                    </thead>
@@ -451,85 +472,108 @@
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
    channels = load_channels()
    if channel_name in channels:
        del channels[channel_name]
        SCORE_CACHE.pop(channel_name, None)
        CHANNEL_FAILOVER_STATE.pop(channel_name, None)
        STREAM_BLACKLIST.pop(channel_name, None)
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
        original_sources = channels.get(channel_name, [])
        for src in original_sources:
            if any(w["url"] == src for w in working):
                best_url = src
                break
        if not best_url:
            best_url = working[0]["url"]

    return {"channel": channel_name, "best_url": best_url, "results": results}
