#!/usr/bin/env node

import { randomBytes } from "node:crypto";
import { createServer } from "node:http";
import { createInterface } from "node:readline";
import { BotGuardClient, getChallenge } from "bgutils-js/botguard";
import { WebPoMinter } from "bgutils-js/webpo";
import { Window } from "happy-dom";
import {
  Constants,
  Innertube,
  Platform,
  UniversalCache,
  YTNodes,
} from "youtubei.js";
import {
  SabrStreamingAdapter,
  SabrUmpProcessor,
} from "googlevideo/sabr-streaming-adapter";
import { MediaHeader, UMPPartId } from "googlevideo/protos";
import { buildSabrFormat, concatenateChunks } from "googlevideo/utils";

// youtubei.js logs through console.info/debug. Keep stdout exclusively for the
// line protocol so the Python parent can parse every response safely.
console.info = (...args) => console.error(...args);
console.debug = (...args) => console.error(...args);
console.warn = (...args) => console.error(...args);
console.log = (...args) => console.error(...args);
const argument = (name, fallback = "") => {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] || fallback : fallback;
};

const cacheDir = argument("--cache-dir");
let client = null;
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
  return new Function(`${data.output}\nreturn { ${properties.join(", ")} }`)();
};

const redact = (value) => String(value)
  .replace(/[\r\n]+/g, " ")
  .replace(/https?:\/\/[^\s]+/gi, "<url>")
  .replace(/(authorization|cookie|sapisid)\s*[:=]\s*[^\s;]+/gi, "$1=<redacted>")
  .slice(0, 500);
const safeError = (error) => redact(error?.message || error || "Resolver failed");

const log = (event, fields = {}) => {
  try {
    const safeFields = Object.fromEntries(
      Object.entries(fields).map(([key, value]) => [
        key,
        typeof value === "string" ? redact(value) : value,
      ]),
    );
    process.stderr.write(
      `[${new Date().toISOString()}] ${event} ${JSON.stringify(safeFields)}\n`,
    );
  } catch {
    // Diagnostics must never break the JSON protocol.
  }
};

const poTokenRequestKey = "O43z0dpjhgX20SCx4KAo";
const nativeFetch = globalThis.fetch.bind(globalThis);

const createFetch = (cookie) => async (input, init = {}) => {
  const headers = new Headers(
    init.headers || (input instanceof Request ? input.headers : undefined),
  );
  if (cookie) headers.set("Cookie", cookie);
  return nativeFetch(input, { ...init, headers });
};

const getPoIntegrityToken = async (botguardResponse, fetch) => {
  const response = await fetch(
    "https://jnn-pa.googleapis.com/$rpc/google.internal.waa.v1.Waa/GenerateIT",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json+protobuf",
        "x-goog-api-key": "[REDACTED_PUBLIC_BOTGUARD_KEY]",
        "x-user-agent": "grpc-web-javascript/0.1",
      },
      body: JSON.stringify([poTokenRequestKey, botguardResponse]),
    },
  );
  if (!response.ok) {
    throw new Error(`PO integrity request failed: ${response.status}`);
  }
  const [integrityToken, estimatedTtlSecs, mintRefreshThreshold, websafeFallbackToken] =
    await response.json();
  return {
    integrityToken,
    estimatedTtlSecs,
    mintRefreshThreshold,
    websafeFallbackToken,
  };
};

const mintWebPoToken = async (client, contentBinding, fetch) => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const window = new Window({ width: 1280, height: 720, console });
  Object.assign(globalThis, { window, document: window.document });
  try {
    const challenge = await getChallenge({
      requestKey: poTokenRequestKey,
      fetchFunction: fetch,
    });
    const interpreterJavascript =
      challenge?.interpreterJavascript
        ?.privateDoNotAccessOrElseSafeScriptWrappedValue;
    if (!challenge?.program || !challenge.globalName) {
      throw new Error("Challenge unavailable");
    }
    const interpreterUrl =
      challenge.interpreterUrl
        ?.privateDoNotAccessOrElseTrustedResourceUrlWrappedValue;
    if (interpreterUrl) {
      const interpreterResponse = await fetch(
        interpreterUrl.startsWith("http") ? interpreterUrl : `https:${interpreterUrl}`,
      );
      if (!interpreterResponse.ok) {
        throw new Error(`BotGuard interpreter failed: ${interpreterResponse.status}`);
      }
      new Function(await interpreterResponse.text())();
    } else if (interpreterJavascript) {
      new Function(interpreterJavascript)();
    }

    const botguard = await BotGuardClient.create({
      program: challenge.program,
      globalName: challenge.globalName,
      globalObject: globalThis,
    });
    try {
      const webPoSignalOutput = [];
      const botguardResponse = await botguard.snapshot({ webPoSignalOutput });
      for (let waitAttempt = 0;
        typeof webPoSignalOutput[0] !== "function" && waitAttempt < 20;
        waitAttempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      log("po_signal_ready", {
        video_id: contentBinding,
        output_count: webPoSignalOutput.length,
        first_output: typeof webPoSignalOutput[0],
      });
      const integrityToken = await getPoIntegrityToken(botguardResponse, fetch);
      const minter = await WebPoMinter.create(integrityToken, webPoSignalOutput);
      return await minter.mintAsWebsafeString(contentBinding);
    } finally {
      await botguard.shutdown().catch(() => {});
    }
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
};

const streamUrls = new Map();
const playheadSeconds = new Map();
const poTokens = new Map();
let streamBase = "";

const pruneStreamUrls = () => {
  const now = Date.now();
  for (const [token, entry] of streamUrls) {
    if (entry.expiresAt <= now) streamUrls.delete(token);
  }
};

const registerStream = (entry, expiresInSeconds, videoId, startMs) => {
  pruneStreamUrls();
  const token = randomBytes(18).toString("base64url");
  streamUrls.set(token, {
    ...entry,
    videoId,
    startMs,
    expiresAt: Date.now() + Math.max(60, expiresInSeconds || 0) * 1000,
  });
  return `${streamBase}/stream/${token}`;
};

const streamResponse = (response, status, message) => {
  response.writeHead(status, { "Content-Type": "text/plain; charset=utf-8" });
  response.end(message);
};

const formatKey = (format) => `${format.itag || ""}:${format.xtags || ""}`;

const readUmpVarint = (data, offset) => {
  if (offset >= data.length) return null;
  const first = data[offset];
  const length = first < 128 ? 1 : first < 192 ? 2 : first < 224 ? 3 : first < 240 ? 4 : 5;
  if (offset + length > data.length) return null;
  if (length === 1) return [first, offset + 1];
  let value;
  if (length === 2) value = (first & 0x3f) + 64 * data[offset + 1];
  else if (length === 3) value = (first & 0x1f) + 32 * (data[offset + 1] + 256 * data[offset + 2]);
  else if (length === 4) value = (first & 0x0f) + 16 * (data[offset + 1] + 256 * (data[offset + 2] + 256 * data[offset + 3]));
  else value = data[offset + 1] + 256 * (data[offset + 2] + 256 * (data[offset + 3] + 256 * data[offset + 4]));
  return [value, offset + length];
};

const readUmpParts = (data) => {
  const parts = [];
  let offset = 0;
  while (offset < data.length) {
    const type = readUmpVarint(data, offset);
    if (!type) break;
    const size = readUmpVarint(data, type[1]);
    if (!size || size[1] + size[0] > data.length) break;
    parts.push({ type: type[0], data: data.slice(size[1], size[1] + size[0]) });
    offset = size[1] + size[0];
  }
  return parts;
};

const readSabrSegments = (data, format) => {
  const pending = new Map();
  const segments = [];
  for (const part of readUmpParts(data)) {
    if (part.type === UMPPartId.MEDIA_HEADER) {
      let header;
      try {
        header = MediaHeader.decode(part.data);
      } catch {
        continue;
      }
      if (formatKey(header) !== formatKey(format)) continue;
      pending.set(header.headerId || 0, { header, chunks: [] });
    } else if (part.type === UMPPartId.MEDIA) {
      const segment = pending.get(part.data[0]);
      if (segment) segment.chunks.push(part.data.slice(1));
    } else if (part.type === UMPPartId.MEDIA_END) {
      const segment = pending.get(part.data[0]);
      if (!segment) continue;
      const segmentData = concatenateChunks(segment.chunks);
      if (segmentData.byteLength) segments.push({ ...segment, data: segmentData });
      pending.delete(part.data[0]);
    }
  }
  return segments;
};

class NodeSabrPlayerAdapter {
  constructor(fetch, videoId) {
    this.fetch = fetch;
    this.videoId = videoId;
    this.requestInterceptors = [];
    this.responseInterceptors = [this.processResponse.bind(this)];
    this.metadata = new Map();
    this.abortController = new AbortController();
    this.cache = null;
    this.requestMetadataManager = null;
    this.fallbackTime = 0;
    this.lastSegmentStartMs = -1;
    this.minimumSegmentStartMs = 0;
  }

  initialize(_player, requestMetadataManager, cache) {
    this.requestMetadataManager = requestMetadataManager;
    this.cache = cache;
  }

  getPlayerTime() {
    return playheadSeconds.get(this.videoId) ?? this.fallbackTime;
  }

  getPlaybackRate() {
    return 1;
  }

  getBandwidthEstimate() {
    return 0;
  }

  getActiveTrackFormats(activeFormat) {
    return activeFormat?.width
      ? { videoFormat: activeFormat }
      : { audioFormat: activeFormat };
  }

  registerRequestInterceptor(interceptor) {
    this.requestInterceptors.push(interceptor);
  }

  registerResponseInterceptor(interceptor) {
    this.responseInterceptors.push(interceptor);
  }

  setFallbackTime(seconds) {
    this.fallbackTime = Math.max(0, Number(seconds) || 0);
  }

  setMinimumSegmentStart(seconds) {
    this.minimumSegmentStartMs = Math.max(0, Number(seconds) || 0) * 1000;
  }

  async processResponse(response) {
    const requestNumber = new URL(response.url).searchParams.get("rn") || "";
    const metadata = this.metadata.get(requestNumber);
    if (!metadata) return response;

    const processor = new SabrUmpProcessor(metadata, this.cache);
    const data = response.data ? new Uint8Array(response.data) : new Uint8Array();
    const result = await processor.processChunk(data);
    if (metadata.error?.sabrError) {
      throw new Error("SABR returned an error");
    }
    if ((metadata.streamInfo?.streamProtectionStatus?.status || 0) >= 3) {
      throw new Error("SABR requires a valid PO token");
    }
    const segments = readSabrSegments(data, metadata.format);
    metadata.sabrSegments = segments;
    const outputSegments = metadata.isInit
      ? segments.filter((segment) => segment.header.isInitSeg)
      : segments.filter((segment) => !segment.header.isInitSeg);
    response.data = outputSegments.length
      ? concatenateChunks(outputSegments.map((segment) => segment.data))
      : result?.data || new Uint8Array();
    if (segments.length) {
      metadata.streamInfo = {
        ...metadata.streamInfo,
        mediaHeader: segments.at(-1).header,
      };
    }
    response.sabrMetadata = metadata;
    log("sabr_response", {
      video_id: this.videoId,
      request: requestNumber,
      init: Boolean(metadata.isInit),
      bytes: response.data.byteLength,
      stream_protection: metadata.streamInfo?.streamProtectionStatus?.status,
      media_start: metadata.streamInfo?.mediaHeader?.startMs,
      media_duration: metadata.streamInfo?.mediaHeader?.durationMs,
      context_update: Boolean(metadata.streamInfo?.sabrContextUpdate),
      reload: Boolean(metadata.streamInfo?.reloadPlaybackContext),
    });
    return response;
  }

  async fetchRequest(url, format, startSeconds, isInit, headers = {}) {
    let request = {
      url,
      method: "GET",
      headers: { accept: "application/vnd.yt-ump", ...headers },
      segment: {
        getStartTime: () => startSeconds,
        isInit: () => isInit,
      },
    };
    for (const interceptor of this.requestInterceptors) {
      request = await interceptor(request);
      if (!request) throw new Error("SABR request was rejected");
    }

    const requestNumber = new URL(request.url).searchParams.get("rn") || "";
    const metadata = this.requestMetadataManager?.getRequestMetadata(request.url)
      || {
        format,
        isInit,
        isUMP: true,
        isSABR: true,
        timestamp: Date.now(),
      };
    metadata.format ||= format;
    metadata.isInit = isInit;
    metadata.isUMP = true;
    metadata.isSABR = true;
    this.metadata.set(requestNumber, metadata);
    try {
      const upstream = await this.fetch(request.url, {
        method: request.method,
        headers: request.headers,
        body: request.body || undefined,
        signal: this.abortController.signal,
      });
      if (!upstream.ok) {
        throw new Error(`SABR request failed: ${upstream.status}`);
      }
      const response = {
        url: request.url,
        method: request.method,
        headers: Object.fromEntries(upstream.headers.entries()),
        data: upstream.body ? await upstream.arrayBuffer() : new ArrayBuffer(),
        makeRequest: (nextUrl, nextHeaders) => this.fetchRequest(
          nextUrl,
          format,
          startSeconds,
          isInit,
          nextHeaders,
        ),
      };
      let filtered = response;
      for (const interceptor of this.responseInterceptors) {
        filtered = await interceptor(filtered) || filtered;
      }
      return filtered;
    } finally {
      this.metadata.delete(requestNumber);
    }
  }

  async fetchSegment(format, startSeconds, isInit = false) {
    const response = await this.fetchRequest(
      `sabr://audio?key=${encodeURIComponent(formatKey(format))}`,
      format,
      startSeconds,
      isInit,
    );
    if (isInit) return response;
    const metadata = response.sabrMetadata;
    const segments = metadata?.sabrSegments || [];
    const fresh = segments.filter((segment) => {
      if (segment.header.isInitSeg) return false;
      const startMs = Number(segment.header.startMs || 0);
      return startMs >= this.minimumSegmentStartMs
        && startMs > (this.lastSegmentStartMs || -1);
    });
    if (!fresh.length) return { ...response, data: new Uint8Array() };
    this.lastSegmentStartMs = Number(fresh.at(-1).header.startMs || 0);
    return {
      ...response,
      data: concatenateChunks(fresh.map((segment) => segment.data)),
      sabrMetadata: {
        ...metadata,
        streamInfo: {
          ...metadata.streamInfo,
          mediaHeader: fresh.at(-1).header,
        },
      },
    };
  }

  dispose() {
    this.abortController.abort();
    this.requestInterceptors = [];
    this.responseInterceptors = [];
    this.metadata.clear();
    this.lastSegmentStartMs = -1;
  }
}

const serveStream = async (request, response) => {
  const token = new URL(request.url || "/", "http://127.0.0.1")
    .pathname.match(/^\/stream\/([A-Za-z0-9_-]+)$/)?.[1];
  const entry = token && streamUrls.get(token);
  if (!entry) {
    streamResponse(response, 404, "Stream not found");
    return;
  }
  try {
    response.writeHead(200, {
      "Content-Type": entry.sabr.mimeType || "audio/webm",
      "Cache-Control": "no-store",
      Connection: "keep-alive",
    });
    const write = async (data) => {
      if (!data?.byteLength || response.destroyed) return;
      if (!response.write(Buffer.from(data))) {
        await new Promise((resolve) => response.once("drain", resolve));
      }
    };

    for await (const chunk of streamSabrAudio(entry, response)) {
      await write(chunk);
      if (response.destroyed) break;
    }
    if (!response.destroyed) response.end();
  } catch (error) {
    log("stream_upstream_error", {
      video_id: entry.videoId,
      error: safeError(error),
    });
    if (response.headersSent) response.destroy();
    else streamResponse(response, 502, safeError(error));
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
      cache: new UniversalCache(false),
      cookie: nextCookie || undefined,
      generate_session_locally: true,
      fetch: createFetch(nextCookie),
    });
    log("client_create_success", {
      client: clientType,
      player_exports: result.session.player?.data?.exported || [],
    });
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
    // Pear uses one browser-like client and the YouTube Music endpoint.
    client = await createClient(value, "WEB");
    cookieHeader = value;
    poTokens.clear();
  }
  return { signed_in: Boolean(cookieHeader) };
};

const bitrate = (format) => Number(
  format.average_bitrate || format.averageBitrate || format.bitrate || 0,
);

const chooseAudioFormat = (formats, qualityKbps) => {
  const audioFormats = formats.filter((format) =>
    String(format.mimeType || "").startsWith("audio/"));
  if (!audioFormats.length) throw new Error("No SABR audio format was returned");
  const target = Math.max(1, Number(qualityKbps) || 320) * 1000;
  const withinTarget = audioFormats.filter((format) => {
    const rate = bitrate(format);
    return rate > 0 && rate <= target;
  });
  const pool = withinTarget.length ? withinTarget : audioFormats;
  const webm = pool.filter((format) =>
    String(format.mimeType || "").includes("webm"));
  const candidates = webm.length ? webm : pool;
  candidates.sort((left, right) => bitrate(right) - bitrate(left));
  return candidates[0];
};

const clientInfo = (activeClient) => {
  const context = activeClient.session.context.client;
  const clientName = Constants.CLIENT_NAME_IDS[context.clientName];
  if (!clientName) throw new Error(`Unsupported SABR client: ${context.clientName}`);
  return {
    clientName: Number(clientName),
    clientVersion: context.clientVersion,
  };
};

const loadPlayerInfo = async (activeClient, videoId, poToken, reloadPlaybackContext) => {
  if (!reloadPlaybackContext) {
    return activeClient.music.getInfo(videoId, { po_token: poToken });
  }
  const endpoint = new YTNodes.NavigationEndpoint({
    watchEndpoint: {
      videoId,
      racyCheckOk: true,
      contentCheckOk: true,
    },
  });
  return endpoint.call(activeClient.actions, {
    playbackContext: {
      adPlaybackContext: { pyv: true },
      contentPlaybackContext: {
        vis: 0,
        splay: false,
        lactMilliseconds: "-1",
        signatureTimestamp: activeClient.session.player?.signature_timestamp,
      },
      reloadPlaybackContext,
    },
    contentCheckOk: true,
    racyCheckOk: true,
    client: "YTMUSIC",
    serviceIntegrityDimensions: { poToken },
    parse: true,
  });
};

const loadSabrData = async (
  activeClient,
  videoId,
  qualityKbps,
  fetch,
  reloadPlaybackContext,
) => {
  let poToken = poTokens.get(videoId);
  if (!poToken || poToken.expiresAt <= Date.now()) {
    let lastError;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        poToken = {
          token: await mintWebPoToken(activeClient, videoId, fetch),
          expiresAt: Date.now() + 5 * 60 * 1000,
        };
        poTokens.set(videoId, poToken);
        break;
      } catch (error) {
        lastError = error;
        log("po_token_attempt_error", {
          video_id: videoId,
          attempt,
          error: safeError(error),
        });
      }
    }
    if (!poToken?.token) throw lastError || new Error("Could not mint PO token");
  }

  const info = await loadPlayerInfo(
    activeClient,
    videoId,
    poToken.token,
    reloadPlaybackContext,
  );
  const streaming = info.streaming_data;
  log("streaming_data", {
    client: "WEB",
    video_id: videoId,
    present: Boolean(streaming),
    formats: streaming?.formats?.length || 0,
    adaptive_formats: streaming?.adaptive_formats?.length || 0,
    server_abr: Boolean(streaming?.server_abr_streaming_url),
    playability: info.playability_status?.status || "",
    reason: info.playability_status?.reason || "",
  });
  if (!streaming?.server_abr_streaming_url) {
    throw new Error("YouTube did not return a SABR streaming URL");
  }

  const ustreamerConfig =
    info.player_config?.media_common_config?.media_ustreamer_request_config
      ?.video_playback_ustreamer_config;
  if (!ustreamerConfig) throw new Error("YouTube did not return a SABR ustreamer config");
  const serverAbrStreamingUrl = await activeClient.session.player?.decipher(
    streaming.server_abr_streaming_url,
  );
  if (!serverAbrStreamingUrl) throw new Error("Could not decipher SABR streaming URL");

  const formats = [
    ...(streaming.formats || []),
    ...(streaming.adaptive_formats || []),
  ].map(buildSabrFormat);
  const audioFormat = chooseAudioFormat(formats, qualityKbps);
  const durationMs = Number(
    info.basic_info?.duration
      || info.video_details?.duration
      || audioFormat.approxDurationMs
      || 0,
  ) * 1000;
  log("format_selected", {
    client: "WEB",
    video_id: videoId,
    itag: audioFormat.itag,
    bitrate: bitrate(audioFormat),
    mime_type: audioFormat.mimeType,
    has_po_token: Boolean(poToken.token),
    duration_ms: durationMs,
  });
  return {
    poToken: poToken.token,
    serverAbrStreamingUrl,
    videoPlaybackUstreamerConfig: ustreamerConfig,
    formats,
    audioFormat,
    mimeType: audioFormat.mimeType || "audio/webm",
    durationMs,
    clientInfo: clientInfo(activeClient),
    expiresInSeconds: streaming.expires instanceof Date
      ? Math.max(0, Math.floor((streaming.expires.getTime() - Date.now()) / 1000))
      : 0,
  };
};

const resolveWithClient = async (activeClient, id, qualityKbps) => {
  const fetch = createFetch(cookieHeader);
  const sabr = await loadSabrData(activeClient, id, qualityKbps, fetch);
  return {
    itag: sabr.audioFormat.itag,
    bitrate: bitrate(sabr.audioFormat),
    mime_type: sabr.mimeType,
    expires_in_seconds: sabr.expiresInSeconds,
    sabr,
    client: activeClient,
    quality_kbps: qualityKbps,
  };
};

/*
 * The adapter requests one media segment at a time. The exact segment start
 * is supplied as clientAbrState.playerTimeMs, which avoids the downloader's
 * fixed 60-second window while keeping mpv's input a normal WebM/MP4 stream.
 */
const streamSabrAudio = async function* (entry, response) {
  const player = new NodeSabrPlayerAdapter(entry.fetch, entry.videoId);
  const adapter = new SabrStreamingAdapter({
    playerAdapter: player,
    clientInfo: entry.sabr.clientInfo,
  });
  adapter.attach({});
  adapter.setStreamingURL(entry.sabr.serverAbrStreamingUrl);
  adapter.setUstreamerConfig(entry.sabr.videoPlaybackUstreamerConfig);
  adapter.setServerAbrFormats(entry.sabr.formats);
  adapter.onMintPoToken(async () => entry.sabr.poToken);
  adapter.onReloadPlayerResponse(async (reloadPlaybackContext) => {
    const refreshed = await loadSabrData(
      entry.client,
      entry.videoId,
      entry.qualityKbps,
      entry.fetch,
      reloadPlaybackContext,
    );
    entry.sabr = refreshed;
    adapter.setStreamingURL(refreshed.serverAbrStreamingUrl);
    adapter.setUstreamerConfig(refreshed.videoPlaybackUstreamerConfig);
    adapter.setServerAbrFormats(refreshed.formats);
  });
  const startSeconds = Math.max(0, Number(entry.startMs || 0) / 1000);
  player.setFallbackTime(startSeconds);
  player.setMinimumSegmentStart(startSeconds);
  playheadSeconds.set(entry.videoId, startSeconds);
  const abortOnClose = () => {
    if (!response.writableEnded) player.dispose();
  };
  response.once("close", abortOnClose);

  try {
    const init = await player.fetchSegment(entry.sabr.audioFormat, startSeconds, true);
    if (init.data?.byteLength) yield init.data;
    let nextSeconds = startSeconds;
    let lastSegmentStartMs = -1;
    const durationSeconds = Number(entry.sabr.durationMs || 0) / 1000;
    while (
      !response.destroyed
      && (!durationSeconds || nextSeconds + 1 < durationSeconds)
    ) {
      const segment = await player.fetchSegment(entry.sabr.audioFormat, nextSeconds);
      if (!segment.data?.byteLength) throw new Error("SABR returned no media data");
      yield segment.data;
      const header = segment.sabrMetadata?.streamInfo?.mediaHeader;
      const startMs = Number(header?.startMs || nextSeconds * 1000);
      const durationMs = Number(header?.durationMs || 0);
      if (!durationMs) throw new Error("SABR returned a segment without duration");
      if (startMs <= lastSegmentStartMs) {
        throw new Error("SABR returned a repeated segment");
      }
      lastSegmentStartMs = startMs;
      const followingSeconds = (startMs + durationMs) / 1000;
      if (durationSeconds && followingSeconds + 0.5 >= durationSeconds) break;
      // SABR advances from the current segment start. Asking at its end skips
      // the next segment because the server treats playerTime as exclusive.
      nextSeconds = startMs / 1000;
    }
  } finally {
    response.off("close", abortOnClose);
    adapter.dispose();
  }
};

const localizeStream = (result, videoId, startMs) => {
  const {
    sabr,
    client,
    quality_kbps: qualityKbps,
    ...metadata
  } = result;
  return {
    ...metadata,
    url: registerStream(
      {
        client,
        qualityKbps,
        fetch: createFetch(cookieHeader),
        sabr,
      },
      result.expires_in_seconds,
      videoId,
      startMs,
    ),
  };
};

const resolve = async ({ video_id: videoId, quality_kbps: qualityKbps, start_ms: requestedStartMs }) => {
  if (!client) await configure(cookieHeader);
  const id = String(videoId || "").trim();
  if (!id) throw new Error("Missing video id");
  const startMs = Math.max(0, Number(requestedStartMs) || 0);
  playheadSeconds.set(id, startMs / 1000);

  try {
    const result = await resolveWithClient(client, id, qualityKbps);
    log("resolve_success", { client: "WEB", video_id: id });
    return localizeStream(result, id, startMs);
  } catch (error) {
    log("resolve_error", {
      client: "WEB",
      video_id: id,
      authenticated: Boolean(cookieHeader),
      error: safeError(error),
    });
    throw error;
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
    case "position": {
      const videoId = String(request?.video_id || "").trim();
      if (videoId) {
        playheadSeconds.set(videoId, Math.max(0, Number(request.position_ms) || 0) / 1000);
      }
      return { updated: Boolean(videoId) };
    }
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
