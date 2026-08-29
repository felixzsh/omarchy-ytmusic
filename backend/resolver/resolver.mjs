#!/usr/bin/env node

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
  .replace(/(authorization|cookie|sapisid)\s*[:=]\s*[^\s;]+/gi, "$1=<redacted>")
  .slice(0, 500);

const createClient = async (nextCookie, clientType) => Innertube.create({
  cache: new UniversalCache(
    true,
    `${cacheDir}/${clientType.toLowerCase()}`,
  ),
  cookie: nextCookie || undefined,
  client_type: clientType,
  generate_session_locally: true,
});

const configure = async (nextCookie) => {
  const value = String(nextCookie || "").trim();
  if (!client || value !== cookieHeader) {
    // Android VR returns regular audio URLs without browser-only SABR
    // requirements. Create the authenticated WEB client only if a restricted
    // track needs the user's session.
    client = await createClient("", "ANDROID_VR");
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

const resolveWithClient = async (activeClient, id, qualityKbps) => {
  const info = await activeClient.getBasicInfo(id);
  const format = chooseAudioFormat(info, qualityKbps);
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

const resolve = async ({ video_id: videoId, quality_kbps: qualityKbps }) => {
  if (!client) await configure(cookieHeader);
  const id = String(videoId || "").trim();
  if (!id) throw new Error("Missing video id");

  try {
    return await resolveWithClient(client, id, qualityKbps);
  } catch (publicError) {
    if (!cookieHeader) throw publicError;
    if (!authenticatedClient) {
      authenticatedClient = await createClient(cookieHeader, "WEB");
    }
    try {
      return await resolveWithClient(authenticatedClient, id, qualityKbps);
    } catch (authenticatedError) {
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
  process.exit(0);
};
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
