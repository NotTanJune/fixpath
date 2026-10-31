import { describe, expect, test } from "bun:test";
import { pageInView, parsePage, stepPage } from "../src/lib/pdfPages";

describe("the page typed into the page indicator", () => {
  test("a number in range is that page", () => {
    expect(parsePage("3", 7)).toBe(3);
    expect(parsePage("  5 ", 7)).toBe(5);
  });

  test("a number out of range is held to the first or the last page", () => {
    expect(parsePage("0", 7)).toBe(1);
    expect(parsePage("99", 7)).toBe(7);
  });

  test("anything that is not a whole number is no page", () => {
    expect(parsePage("", 7)).toBeNull();
    expect(parsePage("abc", 7)).toBeNull();
    expect(parsePage("2.5", 7)).toBeNull();
    expect(parsePage("-1", 7)).toBeNull();
    expect(parsePage("3", 0)).toBeNull();
  });
});

describe("the page the next and previous buttons go to", () => {
  test("it steps by one and stops at the ends", () => {
    expect(stepPage(3, 1, 7)).toBe(4);
    expect(stepPage(3, -1, 7)).toBe(2);
    expect(stepPage(1, -1, 7)).toBe(1);
    expect(stepPage(7, 1, 7)).toBe(7);
  });
});

describe("the page that is in view", () => {
  // three pages of 100 px with 20 px between them, from y = 0
  const spans = [
    { top: 0, bottom: 100 },
    { top: 120, bottom: 220 },
    { top: 240, bottom: 340 },
  ];

  test("it is the page with the most of its height in the view", () => {
    expect(pageInView(spans, 0, 150)).toBe(1);
    expect(pageInView(spans, 90, 240)).toBe(2);
    expect(pageInView(spans, 200, 340)).toBe(3);
  });

  test("a view that only shows the gap takes the nearest page", () => {
    expect(pageInView(spans, 101, 119)).toBe(1);
    expect(pageInView(spans, 221, 239)).toBe(2);
  });

  test("a tie goes to the upper page; no pages is page 1", () => {
    expect(pageInView(spans, 50, 170)).toBe(1);
    expect(pageInView([], 0, 100)).toBe(1);
  });
});
