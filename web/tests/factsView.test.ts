import { expect, test } from "bun:test";
import { fallbackLine, readFactsView, writeFactsView } from "../src/lib/factsView";
import { MAP_VIEW_KEY } from "../src/lib/factMap";

test("the map is the default", () => {
  expect(readFactsView(null)).toBe("map");
  expect(readFactsView({ getItem: () => null })).toBe("map");
  expect(readFactsView({ getItem: () => "nonsense" })).toBe("map");
});

test("a stored list choice is kept", () => {
  expect(readFactsView({ getItem: (k) => (k === MAP_VIEW_KEY ? "list" : null) })).toBe("list");
});

test("storage that throws is the default, and a write that throws is ignored", () => {
  const broken = {
    getItem: () => {
      throw new Error("blocked");
    },
    setItem: () => {
      throw new Error("blocked");
    },
  };
  expect(readFactsView(broken)).toBe("map");
  expect(() => writeFactsView(broken, "list")).not.toThrow();
});

test("a write stores the choice under the key", () => {
  const seen: [string, string][] = [];
  writeFactsView({ setItem: (k, v) => seen.push([k, v]) }, "list");
  expect(seen).toEqual([[MAP_VIEW_KEY, "list"]]);
});

test("the fallback line names why the list shows instead of the map", () => {
  expect(fallbackLine("webgl")).toBe("This browser cannot draw the 3D map, so the facts are listed.");
  expect(fallbackLine("load")).toBe("The map did not load, so the facts are listed.");
});
