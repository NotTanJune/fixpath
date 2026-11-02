import { readFileSync } from "node:fs";
import type { Plugin } from "vite";
import { deflateSync } from "node:zlib";

export interface Brand {
  name: string;
  slug: string;
  tagline: string;
  description: string;
}

interface Tag {
  tag: string;
  attrs?: Record<string, string>;
  children?: string;
  injectTo?: "head" | "head-prepend" | "body" | "body-prepend";
}

const INK = "#1D2023";
const WHITE = "#F9F5D7"; // the ground colour; the mark has no pure white
const BLUE = "#1F4FD1";
const GROUND = "#F9F5D7";
const MAGNETS = ["#C2362B", "#D9921A", "#2D8653", "#2D8653", "#2D8653"];
const DEFAULT_SITE = "http://localhost:5173/";

const FAVICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" fill="${INK}"/>
<rect x="9" y="9" width="14" height="14" fill="${WHITE}"/></svg>`;

/** The tags and the language rule for one brand. A pure function, so a test can read it. */
export function brandPage(brand: Brand, siteUrl: string = DEFAULT_SITE) {
  const base = siteUrl.endsWith("/") ? siteUrl : `${siteUrl}/`;
  const title = `${brand.name} Agentic AI`;
  const meta = (key: "name" | "property", name: string, content: string): Tag => ({
    tag: "meta",
    attrs: { [key]: name, content },
    injectTo: "head",
  });
  const tags: Tag[] = [
    { tag: "title", children: title, injectTo: "head" },
    meta("name", "description", brand.description),
    meta("name", "theme-color", INK),
    meta("name", "robots", "index, follow"),
    meta("property", "og:type", "website"),
    meta("property", "og:site_name", brand.name),
    meta("property", "og:title", title),
    meta("property", "og:description", brand.description),
    meta("property", "og:url", base),
    meta("property", "og:image", `${base}preview.png`),
    meta("property", "og:image:width", "1200"),
    meta("property", "og:image:height", "630"),
    meta("name", "twitter:card", "summary_large_image"),
    meta("name", "twitter:title", title),
    meta("name", "twitter:description", brand.description),
    meta("name", "twitter:image", `${base}preview.png`),
    { tag: "link", attrs: { rel: "canonical", href: base }, injectTo: "head" },
    {
      tag: "link",
      attrs: { rel: "icon", type: "image/svg+xml", href: "/favicon.svg" },
      injectTo: "head",
    },
    {
      tag: "link",
      attrs: { rel: "apple-touch-icon", href: "/apple-touch-icon.png" },
      injectTo: "head",
    },
    {
      tag: "script",
      attrs: { type: "application/ld+json" },
      children: JSON.stringify({
        "@context": "https://schema.org",
        "@type": "WebApplication",
        name: brand.name,
        description: brand.description,
        applicationCategory: "BusinessApplication",
        url: base,
      }),
      injectTo: "head",
    },
  ];
  const setLang = (html: string) =>
    html.replace(
      /<html\b([^>]*)>/i,
      (_match, attrs: string) => `<html lang="en"${attrs.replace(/\slang="[^"]*"/i, "")}>`,
    );
  return { title, tags, setLang };
}

// --- the icons: flat squares, drawn by code so that no image file has to be kept ---------

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes: Uint8Array): number {
  let c = 0xffffffff;
  for (const b of bytes) c = (CRC_TABLE[(c ^ b) & 0xff] as number) ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type: string, data: Uint8Array): Uint8Array {
  const out = new Uint8Array(12 + data.length);
  const view = new DataView(out.buffer);
  view.setUint32(0, data.length);
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
  out.set(data, 8);
  view.setUint32(8 + data.length, crc32(out.subarray(4, 8 + data.length)));
  return out;
}

function hex(color: string): [number, number, number] {
  const n = parseInt(color.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** A PNG of the brand mark: the shop floor tape ground, the white square, and on the wide
 * preview the five magnets with the doubled blue stroke through them. The file says in a
 * text chunk where it came from. */
export function iconPng(width: number, height: number): Uint8Array {
  const px = new Uint8Array(width * height * 3);
  const fill = (x0: number, y0: number, w: number, h: number, color: string) => {
    const [r, g, b] = hex(color);
    for (let y = Math.max(0, y0); y < Math.min(height, y0 + h); y++) {
      for (let x = Math.max(0, x0); x < Math.min(width, x0 + w); x++) {
        const i = (y * width + x) * 3;
        px[i] = r;
        px[i + 1] = g;
        px[i + 2] = b;
      }
    }
  };
  fill(0, 0, width, height, INK);
  if (width >= 600) {
    const unit = Math.round(width / 16);
    const size = unit * 2;
    const gap = Math.round(unit / 2);
    const total = 5 * size + 4 * gap;
    const x0 = Math.round((width - total) / 2);
    const y0 = Math.round((height - size) / 2);
    const mid = y0 + size / 2;
    fill(x0, Math.round(mid - unit / 4), total, Math.round(unit / 2), BLUE);
    fill(x0, Math.round(mid - unit / 8), total, Math.round(unit / 4), INK);
    MAGNETS.forEach((color, i) => fill(x0 + i * (size + gap), y0, size, size, color));
    fill(x0, y0 - unit * 2, Math.round(unit * 0.6), Math.round(unit * 0.6), WHITE);
    fill(x0, y0 + size + unit, total, Math.max(2, Math.round(unit / 12)), GROUND);
  } else {
    const side = Math.round(width * 0.44);
    const offset = Math.round((width - side) / 2);
    fill(offset, offset, side, side, WHITE);
  }
  const rows = new Uint8Array(height * (1 + width * 3));
  for (let y = 0; y < height; y++) {
    rows.set(px.subarray(y * width * 3, (y + 1) * width * 3), y * (1 + width * 3) + 1);
  }
  const header = new Uint8Array(13);
  const view = new DataView(header.buffer);
  view.setUint32(0, width);
  view.setUint32(4, height);
  header.set([8, 2, 0, 0, 0], 8);
  const text = new TextEncoder().encode("Source\0generated by web/brand-plugin.ts");
  const parts = [
    Uint8Array.of(137, 80, 78, 71, 13, 10, 26, 10),
    chunk("IHDR", header),
    chunk("tEXt", text),
    chunk("IDAT", deflateSync(rows)),
    chunk("IEND", new Uint8Array(0)),
  ];
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

const FILES: Record<string, { type: string; body: () => string | Uint8Array }> = {
  "/favicon.svg": { type: "image/svg+xml", body: () => FAVICON_SVG },
  "/apple-touch-icon.png": { type: "image/png", body: () => iconPng(180, 180) },
  "/preview.png": { type: "image/png", body: () => iconPng(1200, 630) },
};

/** Reads ../config/brand.json and puts the head of the page, the icons and the language in. */
export function brandPlugin(options: { siteUrl?: string } = {}): Plugin {
  const brand: Brand = JSON.parse(readFileSync(new URL("../config/brand.json", import.meta.url), "utf8"));
  const page = brandPage(brand, options.siteUrl ?? process.env.SITE_URL ?? DEFAULT_SITE);
  return {
    name: "brand",
    transformIndexHtml: {
      order: "pre",
      handler(html) {
        return { html: page.setLang(html), tags: page.tags };
      },
    },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const file = FILES[(req.url ?? "").split("?")[0] ?? ""];
        if (!file) return next();
        res.setHeader("Content-Type", file.type);
        res.end(Buffer.from(file.body()));
      });
    },
    generateBundle() {
      for (const [path, file] of Object.entries(FILES)) {
        this.emitFile({ type: "asset", fileName: path.slice(1), source: file.body() });
      }
    },
  };
}
