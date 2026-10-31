import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { brandPage, iconPng } from "../brand-plugin.ts";

const brand = JSON.parse(readFileSync(new URL("../../config/brand.json", import.meta.url), "utf8"));
const page = brandPage(brand, "https://demo.example/");

function tag(name: string, key: string, value: string) {
  return page.tags.find((t) => t.tag === name && t.attrs?.[key] === value);
}

describe("brand plugin", () => {
  test("the title carries the name from config/brand.json", () => {
    const title = page.tags.find((t) => t.tag === "title");
    expect(String(title?.children)).toContain(brand.name);
  });

  test("it sets the language of the page", () => {
    expect(page.setLang("<!doctype html><html><head></head></html>")).toContain('<html lang="en">');
    expect(page.setLang('<html lang="fr" class="x">')).toBe('<html lang="en" class="x">');
  });

  test("it injects the description, theme colour and robots tags", () => {
    expect(tag("meta", "name", "description")?.attrs?.content).toBe(brand.description);
    expect(tag("meta", "name", "theme-color")?.attrs?.content).toBe("#1D2023");
    expect(tag("meta", "name", "robots")?.attrs?.content).toContain("index");
  });

  test("it injects the Open Graph and Twitter tags", () => {
    for (const key of ["og:title", "og:description", "og:type", "og:url", "og:image"]) {
      expect(tag("meta", "property", key)).toBeDefined();
    }
    expect(tag("meta", "property", "og:image:width")?.attrs?.content).toBe("1200");
    expect(tag("meta", "property", "og:image:height")?.attrs?.content).toBe("630");
    for (const key of ["twitter:card", "twitter:title", "twitter:description", "twitter:image"]) {
      expect(tag("meta", "name", key)).toBeDefined();
    }
    expect(tag("meta", "name", "twitter:card")?.attrs?.content).toBe("summary_large_image");
  });

  test("it injects the canonical link, the favicon and the apple touch icon", () => {
    expect(tag("link", "rel", "canonical")?.attrs?.href).toBe("https://demo.example/");
    expect(tag("link", "rel", "icon")?.attrs?.href).toBe("/favicon.svg");
    expect(tag("link", "rel", "apple-touch-icon")?.attrs?.href).toBe("/apple-touch-icon.png");
    expect(tag("meta", "property", "og:image")?.attrs?.content).toBe(
      "https://demo.example/preview.png",
    );
  });

  test("it injects structured data that names the app from config/brand.json", () => {
    const script = page.tags.find((t) => t.tag === "script");
    const data = JSON.parse(String(script?.children));
    expect(data.name).toBe(brand.name);
    expect(data["@type"]).toBe("WebApplication");
  });

  test("the icons are PNG files of the right size that say where they came from", () => {
    const png = iconPng(1200, 630);
    expect(Array.from(png.subarray(0, 8))).toEqual([137, 80, 78, 71, 13, 10, 26, 10]);
    const view = new DataView(png.buffer, png.byteOffset);
    expect(view.getUint32(16)).toBe(1200);
    expect(view.getUint32(20)).toBe(630);
    expect(Buffer.from(png).includes("brand-plugin.ts")).toBe(true);
  });
});
