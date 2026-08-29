#!/usr/bin/env node

import { randomBytes } from "node:crypto";
import { createServer } from "node:http";
import { createInterface } from "node:readline";
import { Innertube, Platform, UniversalCache } from "youtubei.js";

// youtubei.js logs through console.info/debug. Keep stdout exclusively for the
// line protocol so the Python parent can parse every response safely.
console.info = (...args) => console.error(...args);
console.debug = (...args) => console.error(...args);
console.warn = (...args) => console.error(...args);

const argument = (name, fallback = "") => {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] || fallback : fallback;
};

const cacheDir = argument("--cache-dir");
let client = null;
let iosClient = null;
let authenticatedClient = null;
let cookieHeader = "";

// youtubei.js deliberately requires applications to provide the evaluator
// used for YouTube's current player transformations. This is the same narrow
// integration used by Pear's downloader and crossfade plugins.
Platform.shim.eval = (data, env) => {
  const properties = [];
  if (env.n) {
    properties.push(`n: exportedVars.nFunction(${JSON.stringify(env.n)})`);
  }
  if (env.sig) {
    properties.push(`sig: exportedVars.sigFunction(${JSON.stringify(env.sig)})`);
  }
  const code = `${data.output}\nreturn { ${properties.join(", ")} }`;
  return new Function(code)();
};

const safeError = (error) => String(error?.message || error || "Resolver failed")
  .replace(/[\r\n]+/g, " ")
  .replace(/https?:\/\/[^\s]+/gi, "<url>")
  .replace(/(authorization|cookie|sapisid)\s*[:=]\s*[^\s;]+/gi, "$1=<redacted>")
  .slice(0, 500);

const log = (event, fields = {}) => {
  try {
    const safeFields = Object.fromEntries(
      Object.entries(fields).map(([key, value]) => [
        key,
        typeof value === "string" ? safeError(value) : value,
      ]),
    );
    process.stderr.write(
      `[${new Date().toISOString()}] ${event} ${JSON.stringify(safeFields)}\n`,
    );
  } catch {
    // Diagnostics must never break the JSON protocol.
  }
};

const streamUrls = new Map();
// YouTube currently rejects open-ended ranges; mpv can consume finite chunks.
const streamChunkBytes = 1024 * 1024;
let streamBase = "";

const pruneStreamUrls = () => {
  const now = Date.now();
  for (const [token, entry] of streamUrls) {
    if (entry.expiresAt <= now) streamUrls.delete(token);
  }
};

const registerStream = (url, expiresInSeconds, videoId) => {
  pruneStreamUrls();
  const token = randomBytes(18).toString("base64url");
  streamUrls.set(token, {
    url,
    videoId,
    expiresAt: Date.now() + Math.max(60, expiresInSeconds || 0) * 1000,
  });
  return `${streamBase}/stream/${token}`;
};

const streamResponse = (response, status, message) => {
  response.writeHead(status, { "Content-Type": "text/plain; charset=utf-8" });
  response.end(message);
};

const serveStream = async (request, response) => {
  const token = new URL(request.url || "/", "http://127.0.0.1")
    .pathname.match(/^\/stream\/([A-Za-z0-9_-]+)$/)?.[1];
  const entry = token && streamUrls.get(token);
  if (!entry) {
    streamResponse(response, 404, "Stream not found");
    return;
  }

  const range = String(request.headers.range || "");
  const match = /^bytes=(\d+)-(\d*)$/.exec(range);
  const start = match ? Number(match[1]) : 0;
  const requestedEnd = match && match[2] ? Number(match[2]) : Infinity;
  if (!Number.isSafeInteger(start) || start < 0 || !Number.isSafeInteger(requestedEnd) && requestedEnd !== Infinity) {
    streamResponse(response, 416, "Invalid range");
    return;
  }
  const end = Math.min(start + streamChunkBytes - 1, requestedEnd);
  if (end < start) {
    streamResponse(response, 416, "Invalid range");
    return;
  }

  try {
    const upstream = await fetch(entry.url, {
      headers: {
        Accept: "*/*",
        Range: `bytes=${start}-${end}`,
        "User-Agent": "Mozilla/5.0",
      },
    });
    if (upstream.status !== 206 || !upstream.body) {
      log("stream_upstream_rejected", {
        video_id: entry.videoId,
        status: upstream.status,
        start,
        end,
      });
      streamResponse(response, 502, "YouTube rejected the audio range");
      return;
    }
    const body = Buffer.from(await upstream.arrayBuffer());
    const contentRange = upstream.headers.get("content-range")
      || `bytes ${start}-${start + body.length - 1}/*`;
    response.writeHead(206, {
      "Accept-Ranges": "bytes",
      "Content-Length": body.length,
      "Content-Range": contentRange,
      "Content-Type": upstream.headers.get("content-type") || "audio/mp4",
      "Cache-Control": "no-store",
    });
    response.end(body);
  } catch (error) {
    log("stream_upstream_error", {
      video_id: entry.videoId,
      start,
      end,
      error: safeError(error),
    });
    streamResponse(response, 502, safeError(error));
  }
};

const streamServer = createServer((request, response) => {
  void serveStream(request, response);
});
await new Promise((resolve, reject) => {
  streamServer.once("error", reject);
  streamServer.listen(0, "127.0.0.1", resolve);
});
streamBase = `http://127.0.0.1:${streamServer.address().port}`;
log("resolver_started", { stream_base: streamBase, cache_dir: cacheDir });

const createClient = async (nextCookie, clientType) => {
  log("client_create_start", {
    client: clientType,
    authenticated: Boolean(nextCookie),
  });
  try {
    const result = await Innertube.create({
      cache: new UniversalCache(
        true,
        `${cacheDir}/${clientType.toLowerCase()}`,
      ),
      cookie: nextCookie || undefined,
      client_type: clientType,
      generate_session_locally: true,
    });
    log("client_create_success", { client: clientType });
    return result;
  } catch (error) {
    log("client_create_error", { client: clientType, error: safeError(error) });
    throw error;
  }
};

const configure = async (nextCookie) => {
  const value = String(nextCookie || "").trim();
  log("configure", {
    authenticated: Boolean(value),
    changed: !client || value !== cookieHeader,
  });
  if (!client || value !== cookieHeader) {
    // Android VR returns regular audio URLs without browser-only SABR
    // requirements. Create the authenticated WEB client only if a restricted
    // track needs the user's session.
    client = await createClient("", "ANDROID_VR");
    iosClient = null;
    authenticatedClient = null;
    cookieHeader = value;
  }
  return { signed_in: Boolean(cookieHeader) };
};

const bitrate = (format) => Number(
  format.average_bitrate || format.bitrate || 0,
);

const chooseAudioFormat = (info, qualityKbps) => {
  const streaming = info.streaming_data;
  if (!streaming) throw new Error("No streaming data was returned");

  const formats = [
    ...(streaming.formats || []),
    ...(streaming.adaptive_formats || []),
  ].filter((format) => {
    const mime = String(format.mime_type || "");
    return (format.has_audio && !format.has_video && mime.startsWith("audio/"))
      || (format.has_audio && format.has_video && mime.startsWith("video/"));
  });

  if (!formats.length) throw new Error("No audio format was returned");

  const target = Math.max(1, Number(qualityKbps) || 320) * 1000;
  const audioOnly = formats.filter((format) => !format.has_video);
  const pool = audioOnly.length ? audioOnly : formats;
  const withinTarget = pool.filter((format) => {
    const rate = bitrate(format);
    return rate > 0 && rate <= target;
  });
  const candidates = withinTarget.length ? withinTarget : pool;
  candidates.sort((left, right) => bitrate(right) - bitrate(left));
  return candidates[0];
};

const resolveWithClient = async (activeClient, clientType, id, qualityKbps) => {
  const info = await activeClient.getBasicInfo(id);
  const streaming = info.streaming_data;
  log("streaming_data", {
    client: clientType,
    video_id: id,
    present: Boolean(streaming),
    formats: streaming?.formats?.length || 0,
    adaptive_formats: streaming?.adaptive_formats?.length || 0,
    server_abr: Boolean(streaming?.server_abr_streaming_url),
    playability: info.playability_status?.status || "",
    reason: info.playability_status?.reason || "",
  });
  const format = chooseAudioFormat(info, qualityKbps);
  log("format_selected", {
    client: clientType,
    video_id: id,
    itag: Number(format.itag || 0),
    bitrate: bitrate(format),
    mime_type: String(format.mime_type || ""),
    has_audio: Boolean(format.has_audio),
    has_video: Boolean(format.has_video),
    has_url: Boolean(format.url),
    has_cipher: Boolean(format.signature_cipher || format.cipher),
  });
  const url = await format.decipher(activeClient.session.player);
  if (!url || !/^https:\/\//.test(url)) throw new Error("Invalid audio URL");

  const expiresAt = info.streaming_data?.expires;
  const expires = expiresAt instanceof Date
    ? Math.max(0, Math.floor((expiresAt.getTime() - Date.now()) / 1000))
    : 0;
  return {
    url,
    itag: Number(format.itag || 0),
    bitrate: bitrate(format),
    mime_type: String(format.mime_type || ""),
    expires_in_seconds: expires,
  };
};

const localizeStream = (result, videoId) => ({
  ...result,
  url: registerStream(result.url, result.expires_in_seconds, videoId),
});

const resolve = async ({ video_id: videoId, quality_kbps: qualityKbps }) => {
  if (!client) await configure(cookieHeader);
  const id = String(videoId || "").trim();
  if (!id) throw new Error("Missing video id");

  try {
    const result = await resolveWithClient(client, "ANDROID_VR", id, qualityKbps);
    log("resolve_success", { client: "ANDROID_VR", video_id: id });
    return localizeStream(result, id);
  } catch (publicError) {
    log("resolve_error", {
      client: "ANDROID_VR",
      video_id: id,
      authenticated: Boolean(cookieHeader),
      error: safeError(publicError),
    });
    try {
      if (!iosClient) iosClient = await createClient("", "iOS");
      const result = await resolveWithClient(iosClient, "iOS", id, qualityKbps);
      log("resolve_success", { client: "iOS", video_id: id });
      return localizeStream(result, id);
    } catch (iosError) {
      log("resolve_error", {
        client: "iOS",
        video_id: id,
        authenticated: false,
        error: safeError(iosError),
      });
      if (!cookieHeader) {
        throw new Error(`${safeError(publicError)}; iOS: ${safeError(iosError)}`);
      }
    }
    if (!authenticatedClient) {
      authenticatedClient = await createClient(cookieHeader, "WEB");
    }
    try {
      const result = await resolveWithClient(authenticatedClient, "WEB", id, qualityKbps);
      log("resolve_success", { client: "WEB", video_id: id });
      return localizeStream(result, id);
    } catch (authenticatedError) {
      log("resolve_error", {
        client: "WEB",
        video_id: id,
        authenticated: true,
        error: safeError(authenticatedError),
      });
      throw new Error(`${safeError(publicError)}; authenticated: ${safeError(authenticatedError)}`);
    }
  }
};

const respond = (id, ok, result = {}, error = "") => {
  process.stdout.write(`${JSON.stringify({
    id,
    ok,
    ...(ok ? { result } : { error: safeError(error) }),
  })}\n`);
};

const handle = async (request) => {
  const id = request?.id;
  switch (String(request?.command || "")) {
    case "configure":
      return configure(request.cookie);
    case "resolve":
      return resolve(request);
    case "ping":
      return { ready: Boolean(client) };
    default:
      throw new Error("Unknown resolver command");
  }
};

let work = Promise.resolve();
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  work = work.then(async () => {
    let request;
    try {
      request = JSON.parse(line);
      const result = await handle(request);
      respond(request?.id, true, result);
    } catch (error) {
      respond(request?.id, false, {}, error);
    }
  });
});

const shutdown = () => {
  input.close();
  streamServer.close();
  process.exit(0);
};
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
