import { describe, expect, test } from "bun:test";
import { nextIndex } from "../src/lib/roving";

describe("roving focus in a chart", () => {
  test("down and right go to the next item, up and left to the previous", () => {
    expect(nextIndex("ArrowDown", 2, 5)).toBe(3);
    expect(nextIndex("ArrowRight", 2, 5)).toBe(3);
    expect(nextIndex("ArrowUp", 2, 5)).toBe(1);
    expect(nextIndex("ArrowLeft", 2, 5)).toBe(1);
  });

  test("the ends stop, they do not wrap", () => {
    expect(nextIndex("ArrowDown", 4, 5)).toBe(4);
    expect(nextIndex("ArrowUp", 0, 5)).toBe(0);
  });

  test("home and end jump to the first and last", () => {
    expect(nextIndex("Home", 3, 5)).toBe(0);
    expect(nextIndex("End", 1, 5)).toBe(4);
  });

  test("any other key, and an empty list, change nothing", () => {
    expect(nextIndex("Tab", 2, 5)).toBeNull();
    expect(nextIndex("a", 2, 5)).toBeNull();
    expect(nextIndex("ArrowDown", 0, 0)).toBeNull();
  });
});
