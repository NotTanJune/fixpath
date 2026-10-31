import { describe, expect, test } from "bun:test";
import { createSseParser } from "../src/lib/sse";

describe("event stream parser", () => {
  test("reads one event with a name and a data line", () => {
    const parser = createSseParser();
    expect(parser.push('event: queued\ndata: {"position":2}\n\n')).toEqual([
      { event: "queued", data: '{"position":2}' },
    ]);
  });

  test("ignores comment lines such as the keepalive", () => {
    const parser = createSseParser();
    const out = parser.push(": ping\n\nevent: done\ndata: {}\n\n: ping\n\n");
    expect(out).toEqual([{ event: "done", data: "{}" }]);
  });

  test("joins data lines of one event with a newline", () => {
    const parser = createSseParser();
    const out = parser.push("event: answer\ndata: line one\ndata: line two\n\n");
    expect(out).toEqual([{ event: "answer", data: "line one\nline two" }]);
  });

  test("holds a partial chunk until the event is complete", () => {
    const parser = createSseParser();
    expect(parser.push("event: tool_ca")).toEqual([]);
    expect(parser.push('ll\ndata: {"name":')).toEqual([]);
    expect(parser.push('"fixes_for"}\n')).toEqual([]);
    expect(parser.push("\n")).toEqual([{ event: "tool_call", data: '{"name":"fixes_for"}' }]);
  });

  test("reads several events from one chunk and keeps the tail", () => {
    const parser = createSseParser();
    const out = parser.push("event: a\ndata: 1\n\nevent: b\ndata: 2\n\nevent: c\ndata: 3");
    expect(out.map((m) => m.event)).toEqual(["a", "b"]);
    expect(parser.push("\n\n")).toEqual([{ event: "c", data: "3" }]);
  });

  test("accepts carriage returns and a space after the colon or none", () => {
    const parser = createSseParser();
    const out = parser.push("event:done\r\ndata:{}\r\n\r\n");
    expect(out).toEqual([{ event: "done", data: "{}" }]);
  });

  test("an event without a name is a message", () => {
    const parser = createSseParser();
    expect(parser.push("data: hello\n\n")).toEqual([{ event: "message", data: "hello" }]);
  });

  test("a block with only comments gives nothing", () => {
    const parser = createSseParser();
    expect(parser.push(": one\n: two\n\n")).toEqual([]);
  });
});
