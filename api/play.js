const CHANNELS_URL =
  "https://raw.githubusercontent.com/kadirsener1/aktif/refs/heads/main/channels.json";
const CACHE_TTL_MS = 60 * 1000;

let cache = {
  data: null,
  expiresAt: 0,
};

async function getChannels() {
  const now = Date.now();
  if (cache.data && now < cache.expiresAt) {
    return cache.data;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);

  try {
    const res = await fetch(CHANNELS_URL, {
      headers: {
        "User-Agent": "Mozilla/5.0",
        "Cache-Control": "no-cache",
      },
      signal: controller.signal,
    });
    clearTimeout(timeout);

    if (!res.ok) {
      throw new Error(`channels.json HTTP ${res.status}`);
    }

    const parsed = await res.json();
    if (parsed && typeof parsed === "object") {
      cache = {
        data: parsed,
        expiresAt: now + CACHE_TTL_MS,
      };
      return parsed;
    }

    return cache.data || {};
  } catch (err) {
    clearTimeout(timeout);
    if (cache.data) {
      return cache.data;
    }
    throw err;
  }
}

async function isStreamWorking(url, timeoutMs = 2500) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(url, {
      method: "GET",
      headers: {
        "User-Agent": "VLC/3.0.18 LibVLC/3.0.18",
        "Range": "bytes=0-1",
      },
      redirect: "follow",
      signal: controller.signal,
    });
    clearTimeout(timeout);
    return res.ok || res.status === 206;
  } catch {
    clearTimeout(timeout);
    return false;
  }
}

export default async function handler(req, res) {
  try {
    let channel = req.query?.channel;

    if (!channel && req.url) {
      const pathOnly = req.url.split("?")[0];
      const match = pathOnly.match(/^\/play\/([^/?#]+)/);
      if (match) {
        channel = decodeURIComponent(match[1]);
      }
    }

    let channels;
    try {
      channels = await getChannels();
    } catch (err) {
      return res.status(502).send(`channels.json okunamadi: ${err.message}`);
    }

    if (!channel || !Object.prototype.hasOwnProperty.call(channels, channel)) {
      return res.status(404).send("Kanal bulunamadi.");
    }

    const urls = channels[channel];
    if (!Array.isArray(urls) || urls.length === 0) {
      return res.status(503).send("Kanal icin link tanimli degil.");
    }

    for (const rawUrl of urls) {
      if (typeof rawUrl === "string" && rawUrl.trim()) {
        const cleanUrl = rawUrl.trim();
        const ok = await isStreamWorking(cleanUrl);
        if (ok) {
          res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate");
          return res.redirect(302, cleanUrl);
        }
      }
    }

    return res.status(503).send("Calisan yayin linki bulunamadi.");
  } catch (err) {
    return res.status(500).send(`Sunucu hatasi: ${err.message}`);
  }
}
