import aiohttp
from aiohttp import web
import asyncio
import json
import os
import re
import time
import base64
from urllib.parse import urljoin

CONFIG_FILE = "channels.json"
STREAM_CACHE = {}
SCORE_CACHE = {}
PROBE_CACHE = {}                   # URL → (height, bandwidth, timestamp)
M3U8_CACHE = {}                    # URL → (content, timestamp)
CACHE_DURATION = 60                # En iyi URL cache (1 dk)
PROBE_CACHE_DURATION = 86400       # Çözünürlük cache (24 saat)
M3U8_CACHE_DURATION = 300          # M3U8 içerik cache (5 dk)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "iptv123")

# ═══════════════════════════════════════════════════════════════
#   H.264 SPS PARSER — Gerçek video çözünürlüğünü tespit eder
# ═══════════════════════════════════════════════════════════════

class BitReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
    def read_bit(self):
        bp = self.pos // 8
        if bp >= len(self.data):
            return 0
        b = (self.data[bp] >> (7 - (self.pos % 8))) & 1
        self.pos += 1
        return b
    def read_bits(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read_bit()
        return v
    def read_ue(self):
        z = 0
        while self.read_bit() == 0 and z < 32:
            z += 1
        return ((1 << z) - 1 + self.read_bits(z)) if z > 0 else 0
    def read_se(self):
        v = self.read_ue()
        return -(v // 2) if v % 2 == 0 else (v + 1) // 2

def _remove_emulation_bytes(data):
    out = bytearray()
    i = 0
    while i < len(data):
        if i + 2 < len(data) and data[i] == 0 and data[i+1] == 0 and data[i+2] == 3:
            out.append(0); out.append(0); i += 3
        else:
            out.append(data[i]); i += 1
    return bytes(out)

def parse_sps(data):
    """H.264 SPS verisinden (width, height) döner."""
    try:
        clean = _remove_emulation_bytes(data)
        br = BitReader(clean)
        profile_idc = br.read_bits(8)
        br.read_bits(8)
        br.read_bits(8)
        br.read_ue()

        if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma = br.read_ue()
            if chroma == 3:
                br.read_bit()
            br.read_ue(); br.read_ue()
            br.read_bit()
            if br.read_bit():
                n = 8 if chroma != 3 else 12
                for i in range(n):
                    if br.read_bit():
                        size = 16 if i < 6 else 64
                        last_scale = next_scale = 8
                        for _ in range(size):
                            if next_scale != 0:
                                next_scale = (last_scale + br.read_se() + 256) % 256
                            last_scale = next_scale if next_scale != 0 else last_scale

        br.read_ue()
        poc = br.read_ue()
        if poc == 0:
            br.read_ue()
        elif poc == 1:
            br.read_bit(); br.read_se(); br.read_se()
            for _ in range(br.read_ue()):
                br.read_se()

        br.read_ue()
        br.read_bit()
        pic_w_mbs = br.read_ue()
        pic_h_map = br.read_ue()
        frame_mbs_only = br.read_bit()

        width = (pic_w_mbs + 1) * 16
        height = (2 - frame_mbs_only) * (pic_h_map + 1) * 16

        if not frame_mbs_only:
            br.read_bit()
        br.read_bit()

        if br.read_bit():
            cl = br.read_ue(); cr = br.read_ue()
            ct = br.read_ue(); cb = br.read_ue()
            width -= (cl + cr) * 2
            height -= (ct + cb) * 2

        if 16 <= width <= 7680 and 16 <= height <= 4320:
            return width, height
    except Exception:
        pass
    return None

def find_sps_resolution(data):
    """Raw veriden H.264 SPS NAL unit arar, çözünürlük döner."""
    best = None
    best_pixels = 0
    i = 0
    limit = len(data) - 5
    while i < limit:
        if data[i] == 0 and data[i+1] == 0:
            if data[i+2] == 1:
                nal_type = data[i+3] & 0x1F
                if nal_type == 7:
                    res = parse_sps(data[i+4:i+4+256])
                    if res:
                        px = res[0] * res[1]
                        if px > best_pixels:
                            best = res; best_pixels = px
                    i += 4; continue
            elif data[i+2] == 0 and data[i+3] == 1:
                nal_type = data[i+4] & 0x1F
                if nal_type == 7:
                    res = parse_sps(data[i+5:i+5+256])
                    if res:
                        px = res[0] * res[1]
                        if px > best_pixels:
                            best = res; best_pixels = px
                    i += 5; continue
        i += 1
    return best

def estimate_height_from_bitrate(bw):
    if bw >= 5000000: return 1080
    if bw >= 2500000: return 720
    if bw >= 1200000: return 480
    if bw >= 600000:  return 360
    if bw > 0:        return 240
    return 0

async def probe_segment(session, segment_url, duration):
    """Segmentin ilk 8 KB'ını indirip çözünürlük + bitrate tespit eder.
    Sonucu 24 saat cache'ler (çözünürlük değişmez)."""
    now = time.time()

    if segment_url in PROBE_CACHE:
        h, bw, ts = PROBE_CACHE[segment_url]
        if now - ts < PROBE_CACHE_DURATION:
            return h, bw

    try:
        headers = {"Range": "bytes=0-8191"}
        async with session.get(segment_url, headers=headers, timeout=3.5) as resp:
            if resp.status not in (200, 206):
                return 0, 0

            total_size = 0
            cr = resp.headers.get('Content-Range', '')
            if '/' in cr:
                try: total_size = int(cr.split('/')[-1])
                except: pass

            data = await resp.read()
            if not total_size:
                total_size = int(resp.headers.get('Content-Length', len(data)))

            bitrate = int((total_size * 8) / duration) if duration > 0 else 0

            sps_res = find_sps_resolution(data)
            if sps_res:
                height = sps_res[1]
            else:
                height = estimate_height_from_bitrate(bitrate)

            PROBE_CACHE[segment_url] = (height, bitrate, now)
            return height, bitrate
    except Exception:
        return 0, 0

# ═══════════════════════════════════════════════════════════════

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
            headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
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
        current = load_channels()
        current.update(channels)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=4)
        if GITHUB_TOKEN and GITHUB_REPO:
            import requests
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CONFIG_FILE}"
            headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
            res = requests.get(url, headers=headers)
            sha = res.json().get("sha") if res.status_code == 200 else None
            cs = json.dumps(current, ensure_ascii=False, indent=4)
            ce = base64.b64encode(cs.encode("utf-8")).decode("utf-8")
            data = {"message": "Admin panelinden channels.json güncellendi ve senkronize edildi",
                    "content": ce, "sha": sha}
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
            u, p = decoded.split(':', 1)
            return u == ADMIN_USER and p == ADMIN_PASS
    except Exception:
        pass
    return False

def basic_auth_required(handler):
    async def wrapper(request):
        if not check_auth(request):
            return web.Response(status=401,
                headers={'WWW-Authenticate': 'Basic realm="IPTV Admin Paneli"'},
                text="Yetkisiz erişim. Lütfen giriş yapın.")
        return await handler(request)
    return wrapper

async def fetch_m3u8(session, url, timeout_sec=5.0):
    """M3U8 içeriğini 5 dakika cache ile çeker. Trafik tasarrufu için."""
    now = time.time()
    if url in M3U8_CACHE:
        content, ts = M3U8_CACHE[url]
        if now - ts < M3U8_CACHE_DURATION:
            return content, 200, 0.0  # Cache hit

    start = time.time()
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with session.get(url, timeout=timeout) as response:
        elapsed = time.time() - start
        if response.status != 200:
            return None, response.status, elapsed
        content = await response.text()
        M3U8_CACHE[url] = (content, now)
        return content, 200, elapsed

async def check_url(session, url):
    """M3U8 URL'sini test eder. Çözünürlük tespiti için SPS parser kullanır."""
    try:
        content, status, elapsed_time = await fetch_m3u8(session, url, 5.0)

        if status != 200:
            return {"url": url, "ok": False, "reason": f"HTTP {status}",
                    "score": 0, "tier": 0, "resolution": 0, "smoothness": 0,
                    "speed": elapsed_time, "resolution_source": "none"}

        if not content or "#EXTM3U" not in content:
            return {"url": url, "ok": False, "reason": "Geçersiz M3U8",
                    "score": 0, "tier": 0, "resolution": 0, "smoothness": 0,
                    "speed": elapsed_time, "resolution_source": "none"}

        max_height = 0
        max_bandwidth = 0
        resolution_source = "manifest"

        resolutions = re.findall(r'RESOLUTION=(\d+)x(\d+)', content)
        if resolutions:
            max_height = max(int(h) for _, h in resolutions)

        bandwidths = re.findall(r'BANDWIDTH=(\d+)', content)
        if bandwidths:
            max_bandwidth = max(int(b) for b in bandwidths)

        is_master = '#EXT-X-STREAM-INF' in content

        # Master playlist: en iyi variant'ı takip et
        if is_master and max_height == 0:
            lines = content.split('\n')
            variant_url = None
            for i, line in enumerate(lines):
                if line.startswith('#EXT-X-STREAM-INF') and i + 1 < len(lines):
                    candidate = lines[i+1].strip()
                    if candidate and not candidate.startswith('#'):
                        variant_url = urljoin(url, candidate)
                        break
            if variant_url:
                try:
                    vcontent, vstatus, _ = await fetch_m3u8(session, variant_url, 3.0)
                    if vstatus == 200 and vcontent:
                        vres = re.findall(r'RESOLUTION=(\d+)x(\d+)', vcontent)
                        if vres:
                            max_height = max(int(h) for _, h in vres)
                        content = vcontent
                        url_for_segments = variant_url
                    else:
                        url_for_segments = url
                except Exception:
                    url_for_segments = url
            else:
                url_for_segments = url
        else:
            url_for_segments = url

        # Hâlâ çözünürlük yoksa segment probe (ilk 8 KB)
        if max_height == 0:
            resolution_source = "sps_probe"
            segment_lines = [ln.strip() for ln in content.split('\n')
                             if ln.strip() and not ln.startswith('#')]
            extinfs = re.findall(r'#EXTINF:([\d.]+)', content)
            if segment_lines and extinfs:
                first_seg = segment_lines[0]
                duration = float(extinfs[0]) if extinfs else 6.0
                if not first_seg.startswith('http'):
                    first_seg = urljoin(url_for_segments, first_seg)
                probed_h, measured_bw = await probe_segment(session, first_seg, duration)
                if probed_h > 0:
                    max_height = probed_h
                if measured_bw > max_bandwidth:
                    max_bandwidth = measured_bw

        # Son çare: bandwidth'ten tahmin
        if max_height == 0:
            resolution_source = "bandwidth_estimate"
            max_height = estimate_height_from_bitrate(max_bandwidth) or 480

        segment_count = len(re.findall(r'#EXTINF:', content))
        has_target_duration = '#EXT-X-TARGETDURATION' in content

        speed_score = max(0.0, 100.0 - elapsed_time * 28.0)

        if max_bandwidth > 0:
            if max_height >= 1080:   expected_bw = 5000000
            elif max_height >= 720:  expected_bw = 2500000
            else:                    expected_bw = 1000000
            bandwidth_score = min((max_bandwidth / expected_bw) * 60.0, 100.0)
        else:
            bandwidth_score = 50.0

        reliability_score = 40.0
        if segment_count > 0:
            reliability_score += min(segment_count * 5.0, 30.0)
        if has_target_duration:
            reliability_score += 15.0
        if is_master:
            reliability_score += 15.0
        reliability_score = min(reliability_score, 100.0)

        smoothness_score = (speed_score * 0.45 + bandwidth_score * 0.30 + reliability_score * 0.25)

        SMOOTH_THRESHOLD = 70.0
        if max_height >= 1080 and smoothness_score >= SMOOTH_THRESHOLD:
            tier = 3
        elif max_height >= 720 and smoothness_score >= SMOOTH_THRESHOLD:
            tier = 2
        elif max_height >= 480 and smoothness_score >= SMOOTH_THRESHOLD:
            tier = 1
        else:
            tier = 0

        final_score = (tier * 1000) + smoothness_score

        return {
            "url": url, "ok": True, "reason": "OK",
            "score": final_score, "tier": tier,
            "resolution": max_height, "resolution_source": resolution_source,
            "bandwidth": max_bandwidth, "smoothness": smoothness_score,
            "speed": elapsed_time, "speed_score": speed_score,
            "bandwidth_score": bandwidth_score, "reliability_score": reliability_score,
            "segments": segment_count, "is_master": is_master
        }
    except asyncio.TimeoutError:
        return {"url": url, "ok": False, "reason": "Timeout",
                "score": 0, "tier": 0, "resolution": 0, "smoothness": 0,
                "speed": 5.0, "resolution_source": "none"}
    except Exception as e:
        return {"url": url, "ok": False, "reason": f"Hata: {str(e)[:40]}",
                "score": 0, "tier": 0, "resolution": 0, "smoothness": 0,
                "speed": 0, "resolution_source": "none"}

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
    async with aiohttp.ClientSession() as session:
        tasks = [check_url(session, u) for u in sources]
        results = await asyncio.gather(*tasks)
    results = [r for r in results if r is not None]
    SCORE_CACHE[channel_name] = {"time": current_time, "results": results}
    working = [r for r in results if r["ok"]]
    if working:
        working.sort(key=lambda x: x["score"], reverse=True)
        STREAM_CACHE[channel_name] = {"url": working[0]["url"], "time": current_time,
                                       "score": working[0]["score"]}
    return results

async def get_working_stream(channel_name):
    current_time = time.time()
    if channel_name in STREAM_CACHE:
        c = STREAM_CACHE[channel_name]
        if current_time - c["time"] < CACHE_DURATION:
            return c["url"]
    results = await test_all_sources(channel_name, force=True)
    working = [r for r in results if r["ok"]]
    if not working:
        return None
    working.sort(key=lambda x: x["score"], reverse=True)
    return working[0]["url"]

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
            STREAM_CACHE.pop(old_name, None)
            SCORE_CACHE.pop(old_name, None)
        channels[channel_name] = urls
        save_and_push_channels(channels)
        STREAM_CACHE.pop(channel_name, None)
        SCORE_CACHE.pop(channel_name, None)
    raise web.HTTPFound('/')

@basic_auth_required
async def admin_delete(request):
    channel_name = request.match_info.get('channel_name')
    channels = load_channels()
    if channel_name in channels:
        del channels[channel_name]
        save_and_push_channels(channels)
        STREAM_CACHE.pop(channel_name, None)
        SCORE_CACHE.pop(channel_name, None)
    raise web.HTTPFound('/')

@basic_auth_required
async def admin_clear_cache(request):
    STREAM_CACHE.clear()
    SCORE_CACHE.clear()
    M3U8_CACHE.clear()
    # PROBE_CACHE temizlenmez — çözünürlük değişmez, trafik israfı olur
    raise web.HTTPFound('/')

@basic_auth_required
async def admin_clear_probe_cache(request):
    PROBE_CACHE.clear()
    raise web.HTTPFound('/')

@basic_auth_required
async def admin_test_channel(request):
    channel_name = request.match_info.get('channel_name')
    channels = load_channels()
    if channel_name not in channels:
        return web.json_response({"error": "Kanal bulunamadı"}, status=404)
    results = await test_all_sources(channel_name, force=True)
    best_url = None
    working = [r for r in results if r["ok"]]
    if working:
        working.sort(key=lambda x: x["score"], reverse=True)
        best_url = working[0]["url"]
    return web.json_response({"channel": channel_name, "best_url": best_url, "results": results})

@basic_auth_required
async def dashboard(request):
    channels = load_channels()
    current_time = time.time()
    channels_json_esc = json.dumps(channels, ensure_ascii=False)

    stream_cache_count = len(STREAM_CACHE)
    probe_cache_count = len(PROBE_CACHE)
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
            .btn-clear {{ background: #0d9488; margin-right: 8px; margin-bottom: 15px; }}
            .btn-clear:hover {{ background: #0f766e; }}
            .btn-clear-probe {{ background: #be123c; margin-bottom: 15px; }}
            .btn-clear-probe:hover {{ background: #9f1239; }}
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
            .stat.src {{ background: #3730a3; color: #c7d2fe; }}
            .tier-badge {{ font-weight: bold; padding: 3px 8px; border-radius: 4px; font-size: 11px; }}
            .tier-3 {{ background: #22c55e; color: #052e16; }}
            .tier-2 {{ background: #3b82f6; color: #0c1a3a; }}
            .tier-1 {{ background: #f59e0b; color: #431407; }}
            .tier-0 {{ background: #6b7280; color: #0f172a; }}
            .loading {{ color: #94a3b8; font-style: italic; padding: 10px; }}
            .stats-bar {{ display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 15px; }}
            .stat-item {{ background: #1e293b; padding: 10px 15px; border-radius: 6px; font-size: 13px; }}
            .stat-item strong {{ color: #38bdf8; font-size: 16px; display: block; }}
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
                        const t = td.textContent || td.innerText;
                        trs[i].style.display = (t.toLowerCase().indexOf(input) > -1) ? "" : "none";
                    }}
                }}
            }}

            function tierLabel(tier) {{
                return {{3: "1080p+ ✅", 2: "720p ✅", 1: "480p ✅", 0: "Zayıf ⚠️"}}[tier];
            }}

            function sourceLabel(src) {{
                return {{
                    "manifest": "📄 Manifest",
                    "sps_probe": "🎞️ SPS Parse",
                    "bandwidth_estimate": "📊 Bitrate Tahmin",
                    "none": "❓"
                }}[src] || src;
            }}

            function statClass(value, good, mid) {{
                if (value >= good) return "good";
                if (value >= mid) return "mid";
                return "bad";
            }}

            async function testChannel(name) {{
                const container = document.getElementById('scores-' + name);
                container.classList.add('open');
                container.innerHTML = '<div class="loading">🔄 Linkler test ediliyor...</div>';

                try {{
                    const res = await fetch('/admin/test/' + name);
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
                        if (isBest) html += '🏆 SEÇİLDİ ';
                        if (!r.ok) html += '❌ ';
                        html += 'Skor: ' + r.score.toFixed(1) + '</strong>';
                        if (r.ok) {{
                            html += '<span class="tier-badge tier-' + r.tier + '">' + r.resolution + 'p — ' + tierLabel(r.tier) + '</span>';
                        }} else {{
                            html += '<span class="tier-badge tier-0">' + r.reason + '</span>';
                        }}
                        html += '</div>';
                        html += '<div class="score-url">' + r.url + '</div>';
                        if (r.ok) {{
                            html += '<div class="score-stats">';
                            html += '<span class="stat ' + statClass(r.smoothness, 70, 50) + '">Akıcılık: ' + r.smoothness.toFixed(1) + '</span>';
                            html += '<span class="stat ' + statClass(100 - r.speed*28, 70, 50) + '">Hız: ' + (r.speed*1000).toFixed(0) + ' ms</span>';
                            html += '<span class="stat">Çözünürlük: ' + r.resolution + 'p</span>';
                            html += '<span class="stat src">' + sourceLabel(r.resolution_source) + '</span>';
                            if (r.bandwidth > 0) html += '<span class="stat">Bitrate: ' + (r.bandwidth/1000000).toFixed(2) + ' Mbps</span>';
                            html += '<span class="stat">Segment: ' + r.segments + '</span>';
                            if (r.is_master) html += '<span class="stat good">Master</span>';
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
            <p class="subtitle">H.264 SPS parse ile gerçek çözünürlük + kademeli seçim (1080→720→480) + trafik optimize cache.</p>

            <div class="stats-bar">
                <div class="stat-item"><strong>{len(channels)}</strong>Tanımlı Kanal</div>
                <div class="stat-item"><strong>{stream_cache_count}</strong>Aktif Yayın (1 dk)</div>
                <div class="stat-item"><strong>{m3u8_cache_count}</strong>M3U8 Cache (5 dk)</div>
                <div class="stat-item"><strong>{probe_cache_count}</strong>Çözünürlük Cache (24 saat)</div>
            </div>
            
            <a href="/admin/clear-cache" class="btn-action btn-clear" onclick="return confirm('Yayın ve M3U8 cache temizlensin mi?')">🧹 Yayın + M3U8 Cache Temizle</a>
            <a href="/admin/clear-probe-cache" class="btn-action btn-clear-probe" onclick="return confirm('Çözünürlük cache temizlensin mi? (Yeni testler trafik tüketir)')">🎞️ Çözünürlük Cache Temizle</a>

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
            score_text = f" (Skor: {cache_info.get('score', 0):.1f})"
            status_html = '<span class="badge-cached">Önbellekte Aktif</span>'
            active_url = cache_info["url"] + score_text
        else:
            status_html = '<span class="badge-inactive">Test Bekliyor</span>'
            active_url = f"{len(sources)} kaynak tanımlı - test için 'Skorlar' tıkla"
        
        html += f"""
                        <tr>
                            <td><strong>{name}</strong></td>
                            <td>{status_html}</td>
                            <td>
                                <a href="{play_link}" target="_blank">{play_link}</a><br>
                                <small style="color: #94a3b8; word-break: break-all;">{active_url}</small>
                                <div id="scores-{name}" class="score-detail"></div>
                            </td>
                            <td>
                                <button class="btn-test" onclick="toggleScores('{name}')">📊 Skorlar</button>
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
app.router.add_get('/admin/clear-probe-cache', admin_clear_probe_cache)
app.router.add_get('/admin/test/{channel_name}', admin_test_channel)
app.router.add_get('/ping', ping)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, port=port)
