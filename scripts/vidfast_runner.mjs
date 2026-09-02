#!/usr/bin/env node
// Headless VidFast resolver. It runs the site's player decoder in a Node VM
// and returns the unlocked media URL as JSON on stdout.

import vm from "node:vm";
import { webcrypto } from "node:crypto";

const inputUrl = process.argv[2];
const debug = process.env.VIDFAST_DEBUG === "1";
const log = (...args) => {
  if (debug) console.error("[vidfast]", ...args);
};

if (!inputUrl) {
  console.log(JSON.stringify({ error: "usage: vidfast_runner.mjs <url>" }));
  process.exit(2);
}

const pageUrl = new URL(inputUrl).href;
const pageOrigin = new URL(pageUrl).origin;
const userAgent =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36";
const nativeFetch = globalThis.fetch.bind(globalThis);
const nativeCrypto = globalThis.crypto ?? webcrypto;
const cookies = new Map();
let proxyDispatcher = null;

// Node 18 provides Blob but not the browser-compatible File global.  VidFast's
// player bundle only needs the File shape exposed by the browser VM.
const NativeFile = globalThis.File ?? class File extends Blob {
  constructor(bits, name, options = {}) {
    super(bits, options);
    this.name = String(name);
    this.lastModified = Number(options.lastModified ?? Date.now());
  }

  get [Symbol.toStringTag]() {
    return "File";
  }
};

if (process.env.VIDFAST_PROXY && /^https?:\/\//i.test(process.env.VIDFAST_PROXY)) {
  try {
    const { ProxyAgent } = await import("undici");
    proxyDispatcher = new ProxyAgent(process.env.VIDFAST_PROXY);
  } catch (error) {
    log("HTTP proxy unavailable:", error.message);
  }
}

function fetchOptions(options = {}) {
  return proxyDispatcher ? { ...options, dispatcher: proxyDispatcher } : options;
}

function storeCookies(response) {
  const values = response.headers.getSetCookie?.() || [];
  const raw = values.length ? values.join(",") : response.headers.get("set-cookie") || "";
  for (const item of raw.split(/,(?=\s*[^;,]+=[^;,]+)/)) {
    const first = item.split(";", 1)[0].trim();
    const index = first.indexOf("=");
    if (index > 0) cookies.set(first.slice(0, index), first.slice(index + 1));
  }
}

function cookieHeader() {
  return [...cookies.entries()].map(([key, value]) => `${key}=${value}`).join("; ");
}

function absoluteUrl(value, base = pageUrl) {
  return new URL(String(value), base).href;
}

function mergedHeaders(initHeaders = {}, referer = pageUrl) {
  const headers = new Headers(initHeaders);
  if (!headers.has("user-agent")) headers.set("user-agent", userAgent);
  headers.set("referer", referer);
  headers.set("origin", pageOrigin);
  const cookie = cookieHeader();
  if (cookie) headers.set("cookie", cookie);
  return headers;
}

async function fetchPage() {
  const response = await nativeFetch(pageUrl, fetchOptions({
    headers: mergedHeaders({
      accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "accept-language": "en-US,en;q=0.9",
      "upgrade-insecure-requests": "1",
    }),
  }));
  storeCookies(response);
  if (!response.ok) throw new Error(`VidFast page returned HTTP ${response.status}`);
  return response.text();
}

function parseProps(html) {
  const tokenMatch = html.match(/\\"en\\":\\"([^\\"]+)\\"/);
  if (!tokenMatch) throw new Error("VidFast session token not found");
  const start = html.indexOf(tokenMatch[0]);
  const chunk = html.slice(start, start + 2500);
  const end = chunk.match(/\\"server\\":(?:\\"[^\\"]*\\"|null)\}/);
  if (!end) throw new Error("VidFast player payload is incomplete");
  const raw = chunk.slice(0, end.index + end[0].length)
    .replace(/\\"/g, '"')
    .replace(/"\$undefined"/g, "null");
  try {
    return JSON.parse(`{${raw.slice(0, -1)}}`);
  } catch (error) {
    throw new Error(`VidFast player payload is invalid: ${error.message}`);
  }
}

function scriptUrls(html) {
  const urls = [];
  const seen = new Set();
  for (const match of html.matchAll(/<script[^>]+src="([^"]+)"/gi)) {
    const src = absoluteUrl(match[1]);
    if (!src.includes("/_next/static/chunks/") || seen.has(src)) continue;
    seen.add(src);
    urls.push(src);
  }
  return urls;
}

// Small DOM implementation. The VidFast player decoder only needs browser
// globals while its React/UI side remains stubbed below.
function element(tag = "div") {
  const node = {
    tagName: String(tag).toUpperCase(), style: {}, dataset: {}, children: [],
    parentNode: null, parentElement: null, _attrs: {}, _listeners: {},
    _id: "", _text: "", _html: "", src: "", href: "", paused: true,
    currentTime: 0, duration: 0, volume: 1, muted: false, playbackRate: 1,
    textTracks: [], clientWidth: 1920, clientHeight: 1080,
  };
  Object.defineProperty(node, "id", { get: () => node._id, set: value => { node._id = String(value); } });
  Object.defineProperty(node, "textContent", { get: () => node._text, set: value => { node._text = String(value ?? ""); } });
  Object.defineProperty(node, "innerHTML", { get: () => node._html, set: value => { node._html = String(value ?? ""); } });
  node.setAttribute = (key, value) => { node._attrs[key] = String(value); if (key === "id") node._id = String(value); };
  node.getAttribute = key => node._attrs[key] ?? (key === "id" ? node._id : null);
  node.removeAttribute = key => { delete node._attrs[key]; };
  node.hasAttribute = key => key in node._attrs;
  node.appendChild = child => { if (child) { node.children.push(child); child.parentNode = node; child.parentElement = node; } return child; };
  node.removeChild = child => { const i = node.children.indexOf(child); if (i >= 0) node.children.splice(i, 1); return child; };
  node.remove = () => { if (node.parentNode) node.parentNode.removeChild(node); };
  node.insertBefore = child => node.appendChild(child);
  node.replaceChild = (child, oldChild) => { const i = node.children.indexOf(oldChild); if (i >= 0) node.children[i] = child; return oldChild; };
  node.insertAdjacentHTML = (_, html) => { node._html += String(html ?? ""); };
  node.cloneNode = () => element(tag);
  node.querySelector = () => null;
  node.querySelectorAll = () => [];
  node.getElementsByTagName = () => [];
  node.getElementsByClassName = () => [];
  node.contains = () => false;
  node.matches = () => false;
  node.closest = () => null;
  node.scrollIntoView = () => {};
  node.getBoundingClientRect = () => ({ left: 0, top: 0, right: 1920, bottom: 1080, width: 1920, height: 1080 });
  node.classList = { values: new Set(), add(value) { this.values.add(value); }, remove(value) { this.values.delete(value); }, contains(value) { return this.values.has(value); }, toggle(value) { if (this.values.has(value)) this.values.delete(value); else this.values.add(value); } };
  node.addEventListener = (name, callback) => { (node._listeners[name] ||= []).push(callback); };
  node.removeEventListener = () => {};
  node.dispatchEvent = () => true;
  node.click = () => {};
  node.focus = () => {};
  node.blur = () => {};
  node.play = async () => { node.paused = false; };
  node.pause = () => { node.paused = true; };
  node.load = () => {};
  node.append = (...items) => items.forEach(item => node.appendChild(item));
  return node;
}

const body = element("body");
const head = element("head");
const html = element("html");
const ids = new Map();
const location = {
  href: pageUrl, origin: pageOrigin, protocol: new URL(pageUrl).protocol,
  host: new URL(pageUrl).host, hostname: new URL(pageUrl).hostname,
  pathname: new URL(pageUrl).pathname, search: new URL(pageUrl).search,
  hash: new URL(pageUrl).hash, reload() {}, replace() {}, assign() {},
  toString: () => pageUrl,
};
const document = {
  body, head, documentElement: html, readyState: "complete", location,
  createElement: tag => element(tag), createTextNode: text => { const node = element("#text"); node.textContent = text; return node; },
  getElementById: id => { if (!ids.has(id)) { const node = element(); node.id = id; ids.set(id, node); } return ids.get(id); },
  querySelector: selector => selector === "body" ? body : selector === "head" ? head : null,
  querySelectorAll: () => [], getElementsByTagName: () => [], getElementsByClassName: () => [],
  addEventListener() {}, removeEventListener() {}, cookie: "",
};
const localValues = new Map();
const localStorage = { getItem: key => localValues.get(key) ?? null, setItem: (key, value) => localValues.set(key, String(value)), removeItem: key => localValues.delete(key), clear: () => localValues.clear() };
const sessionStorage = { ...localStorage };
const navigator = {
  userAgent, platform: "Win32", language: "en-US", languages: ["en-US", "en"],
  vendor: "Google Inc.", plugins: { length: 5, namedItem: () => ({}) }, mimeTypes: [],
  webdriver: false, maxTouchPoints: 0, hardwareConcurrency: 8,
  storage: { estimate: async () => ({ quota: 2147483648, usage: 0 }) },
};

function nativeLikeConsole() {
  const base = console;
  return new Proxy(base, { get(target, key) { return ["log", "table", "clear"].includes(key) ? () => {} : target[key]; } });
}

const context = {
  console: nativeLikeConsole(), setTimeout, clearTimeout, setInterval, clearInterval,
  setImmediate, clearImmediate, queueMicrotask, structuredClone,
  Buffer, URL, URLSearchParams, TextEncoder, TextDecoder,
  atob: value => Buffer.from(value, "base64").toString("binary"),
  btoa: value => Buffer.from(value, "binary").toString("base64"),
  AbortController, AbortSignal, Request, Response, Headers, FormData,
  ReadableStream, Blob, File: NativeFile, TextEncoderStream, TextDecoderStream,
  WebAssembly, crypto: nativeCrypto, navigator, location, document,
  localStorage, sessionStorage, window: null, self: null, globalThis: null, global: null,
  performance: globalThis.performance,
  history: { pushState() {}, replaceState() {}, back() {}, forward() {}, go() {}, state: null, length: 1 },
  screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24, pixelDepth: 24 },
  innerWidth: 1920, innerHeight: 1080, outerWidth: 1920, outerHeight: 1080,
  devicePixelRatio: 1, pageXOffset: 0, pageYOffset: 0, scrollX: 0, scrollY: 0,
  top: null, parent: null, frames: null, opener: null, closed: false,
  addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true, postMessage() {},
  requestAnimationFrame: callback => setTimeout(callback, 0), cancelAnimationFrame: clearTimeout,
  requestIdleCallback: callback => setTimeout(() => callback({ didTimeout: false, timeRemaining: () => 50 }), 0),
  cancelIdleCallback: clearTimeout, matchMedia: () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }),
  getComputedStyle: () => ({ getPropertyValue: () => "", getPropertyPriority: () => "", cssText: "" }),
  confirm: () => true, alert: () => {}, prompt: () => null, focus() {}, blur() {},
  Window: function Window() {}, Document: function Document() {}, HTMLDocument: function HTMLDocument() {},
  HTMLElement: function HTMLElement() {}, Node: function Node() {}, Element: function Element() {},
  HTMLDivElement: function HTMLDivElement() {},
  Image: function Image() { return element("img"); }, Audio: function Audio() { return element("audio"); },
  Worker: function Worker() { return { postMessage() {}, terminate() {}, addEventListener() {}, removeEventListener() {} }; },
  MessageChannel: function MessageChannel() { return { port1: { postMessage() {}, start() {}, addEventListener() {} }, port2: { postMessage() {}, start() {}, addEventListener() {} } }; },
  MutationObserver: function MutationObserver() { return { observe() {}, disconnect() {} }; },
  MediaSource: class {}, BroadcastChannel: class { postMessage() {} close() {} addEventListener() {} },
  WebSocket: class { send() {} close() {} addEventListener() {} },
  XMLHttpRequest: class { open() {} send() {} setRequestHeader() {} addEventListener() {} },
};
context.window = context; context.self = context; context.globalThis = context; context.global = context;
context.top = context; context.parent = context;
vm.createContext(context);

let routePrefix = "";
let probeBody = null;
async function playerFetch(input, init = {}) {
  const url = absoluteUrl(input);
  const headers = mergedHeaders(init.headers, pageUrl);
  if (routePrefix && url.includes(routePrefix)) {
    headers.set("accept", "*/*");
    headers.set("x-requested-with", "XMLHttpRequest");
    headers.set("x-csrf-token", context.__playerCsrf || "");
  }
  log("fetch", init.method || "GET", url);
  const response = await nativeFetch(url, fetchOptions({ ...init, headers }));
  storeCookies(response);
  if ((init.method || "GET").toUpperCase() === "POST" && routePrefix && url.includes(routePrefix) && !probeBody && response.ok) {
    probeBody = await response.clone().text();
  }
  return response;
}
context.fetch = playerFetch;

const modules = {};
const cache = {};
function defineExports(exports, map) {
  for (const [key, getter] of Object.entries(map)) Object.defineProperty(exports, key, { enumerable: true, get: getter });
}
function webpackRequire(id) {
  if (cache[id]) return cache[id].exports;
  if (!modules[id]) throw new Error(`missing webpack module ${id}`);
  const mod = { exports: {} };
  cache[id] = mod;
  const req = Object.assign(requested => webpackRequire(requested), {
    d: defineExports, bind: (target, ...args) => target.bind(...args), g: context,
  });
  modules[id](mod, mod.exports, req);
  return mod.exports;
}

function patchPlayerChunk(source) {
  let patched = source;
  patched = patched.replace(
    "cd._0x187b37=cI,globalThis._0x187b37=cd._0x187b37",
    "cd._0x187b37=cI,globalThis.__playerInit=cI,globalThis._0x187b37=cd._0x187b37",
  );
  patched = patched.replace(
    "cd._0x272adf=cK,globalThis._0x272adf=cd._0x272adf",
    "cd._0x272adf=cK,globalThis.__playerDecrypt=cK,globalThis._0x272adf=cd._0x272adf",
  );
  patched = patched.replace(
    'join("")}cr.from("xZ/aW~D6:U0_]EVA");',
    'join("")}globalThis.__playerEncode=ci;cr.from("xZ/aW~D6:U0_]EVA");',
  );
  patched = patched.replace(
    "function c6(e,t){return e-=123,c2()[e]}",
    "function c6(e,t){return e-=123,c2()[e]}globalThis.__playerRoutePrefix=c6(185);globalThis.__playerRouteSegment=c6(449);globalThis.__playerCsrf=JSON.parse(c6(674))[\"X-Csrf-Token\"];",
  );
  return patched;
}

function loadChunk(code, filename) {
  const queue = [];
  context.webpackChunk_N_E = queue;
  try {
    vm.runInContext(code, context, { filename, timeout: 120000 });
  } catch (error) {
    throw new Error(`${filename}: ${error.message} (line ${error.lineNumber || "?"}, column ${error.columnNumber || "?"})`);
  }
  for (const chunk of queue) {
    if (Array.isArray(chunk?.[1])) Object.assign(modules, chunk[1]);
    else if (chunk?.[1]) Object.assign(modules, chunk[1]);
  }
}

const reactStub = new Proxy(function ReactStub() {}, {
  get: (_, key) => {
    if (key === "__esModule") return true;
    if (key === "default") return reactStub;
    if (key === "useState") return initial => [initial, () => {}];
    if (key === "useEffect" || key === "useLayoutEffect") return () => {};
    if (key === "useRef") return initial => ({ current: initial });
    if (key === "useCallback") return fn => fn;
    if (key === "useMemo") return fn => fn();
    if (key === "Fragment") return "Fragment";
    return () => ({});
  },
});
const moduleStubs = {
  5155: reactStub, 63: reactStub, 2115: reactStub,
  8288: { useRouter: () => ({ push() {}, replace() {}, prefetch() {} }), usePathname: () => location.pathname },
  8613: {}, 6497: {}, 4352: {}, 3396: {}, 6368: {}, 5216: {},
  153: { hb: () => ({ pause() {}, start() {}, reset() {} }) },
  2421: { f: async () => ({ cues: [] }) },
};

function installStubs() {
  modules[5376] = mod => { mod.exports = { Buffer }; };
  modules[7358] = mod => { mod.exports = { env: {}, versions: { chrome: "152.0.0.0" }, browser: true }; };
  for (const [id, value] of Object.entries(moduleStubs)) modules[id] = (mod, exports, req) => {
    mod.exports = value;
    if (req?.d) req.d(exports, { default: () => value, __esModule: () => true });
  };
}

async function loadPlayer(html) {
  const urls = scriptUrls(html);
  if (!urls.some(url => /\/365-[^/]+\.js$/.test(url))) throw new Error("VidFast player bundle not found");
  log("loading", urls.length, "chunks");
  for (const url of urls) {
    const response = await nativeFetch(url, fetchOptions({ headers: mergedHeaders({}, pageUrl) }));
    if (!response.ok) continue;
    let code = await response.text();
    if (/\/365-[^/]+\.js$/.test(url)) code = patchPlayerChunk(code);
    loadChunk(code, url);
  }
  installStubs();
  webpackRequire(9987);
  if (typeof context.__playerInit !== "function" || typeof context.__playerDecrypt !== "function" || typeof context.__playerEncode !== "function") {
    throw new Error("VidFast player exports not found; bundle layout changed");
  }
  routePrefix = String(context.__playerRoutePrefix || "");
  if (!routePrefix || !context.__playerRouteSegment) throw new Error("VidFast player routes not found");
  log("route", routePrefix, context.__playerRouteSegment);
  return webpackRequire(3018);
}

function playerContext(props, cryptoModule, servers) {
  return {
    crypto: cryptoModule, encode: context.__playerEncode, en: props.en,
    server: props.server ?? null, setServers: value => {
      const previous = servers.at(-1) || [];
      const rows = typeof value === "function" ? value(previous) : value;
      if (Array.isArray(rows)) servers.push(structuredClone(rows));
    }, setState() {}, setFavServer() {},
    window: context, document, navigator, localStorage, console: nativeLikeConsole(), JSON,
    Math, Date, RegExp, Map, Set, WeakMap, WeakSet, Array, Object, Number, String,
    Boolean, Symbol, Function, screen: context.screen, Error, TypeError, RangeError,
    SyntaxError, parseInt, parseFloat, isNaN, isFinite, encodeURIComponent, decodeURIComponent,
    NaN, Infinity, undefined, Promise, Proxy, Reflect, Uint8Array, Int8Array, Uint16Array,
    Int16Array, Uint32Array, Int32Array, Float32Array, Float64Array, BigInt,
    fetch: playerFetch, TextEncoder, TextDecoder, URL, URLSearchParams, AbortSignal,
    AbortController, Buffer, atob: context.atob, btoa: context.btoa, Worker: context.Worker,
    MessageChannel: context.MessageChannel, ...props, id: props.id || new URL(pageUrl).pathname.split("/").pop(),
    host: props.host || new URL(pageUrl).host,
  };
}

async function resolve() {
  const html = await fetchPage();
  const props = parseProps(html);
  const cryptoModule = await loadPlayer(html);
  const servers = [];
  const ctx = playerContext(props, cryptoModule, servers);
  for (const key of ["crypto", "encode", "en", "server", "setServers", "setState", "setFavServer", "fetch"]) context[key] = ctx[key];
  await context.__playerInit(ctx);

  const deadline = Date.now() + 45000;
  while (!servers.length && Date.now() < deadline) await new Promise(resolvePromise => setTimeout(resolvePromise, 250));
  if (!probeBody) throw new Error("VidFast did not return the server probe");
  const decrypted = [];
  await context.__playerDecrypt({ ...ctx, dr: decrypted, rs: probeBody });
  const active = Array.isArray(decrypted[0]) ? decrypted[0] : [];
  if (!active.length) throw new Error("VidFast returned no servers");

  const errors = [];
  for (const server of active.filter(item => item?.data)) {
    try {
      const endpoint = `${routePrefix}/${context.__playerRouteSegment}/${server.data}`.replace(/\/+/g, "/");
      const response = await playerFetch(endpoint, { method: "POST", body: "" });
      if (!response.ok) throw new Error(`server HTTP ${response.status}`);
      const decryptedStream = [];
      await context.__playerDecrypt({ ...ctx, dr: decryptedStream, rs: (await response.text()).trim(), server });
      const stream = decryptedStream[0];
      if (stream?.url?.startsWith("http")) {
        return { url: stream.url, headers: { "User-Agent": userAgent, Referer: pageUrl, Origin: pageOrigin }, server: server.name || "VidFast" };
      }
      throw new Error("decrypted response has no URL");
    } catch (error) {
      errors.push(`${server.name || "server"}: ${error.message}`);
    }
  }
  throw new Error(errors.join("; ") || "VidFast has no usable server");
}

try {
  console.log(JSON.stringify(await resolve()));
} catch (error) {
  if (debug && error?.stack) console.error(error.stack);
  console.log(JSON.stringify({ error: error?.message || String(error) }));
  process.exitCode = 1;
} finally {
  if (proxyDispatcher) {
    try { await proxyDispatcher.close(); } catch {}
  }
}
