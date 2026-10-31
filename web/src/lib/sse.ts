export interface SseMessage {
  event: string;
  data: string;
}

/** A parser for a server-sent event stream. push() takes any piece of the stream, split
 * anywhere, and gives back the events that are now complete. Comment lines (the
 * keepalive) are dropped, and the data lines of one event are joined with a newline. */
export function createSseParser() {
  let pending = "";

  function block(text: string): SseMessage | null {
    let event = "message";
    const data: string[] = [];
    for (const line of text.split("\n")) {
      if (line === "" || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    return data.length ? { event, data: data.join("\n") } : null;
  }

  return {
    push(chunk: string): SseMessage[] {
      pending += chunk.replace(/\r\n?/g, "\n");
      const out: SseMessage[] = [];
      let end = pending.indexOf("\n\n");
      while (end !== -1) {
        const message = block(pending.slice(0, end));
        if (message) out.push(message);
        pending = pending.slice(end + 2);
        end = pending.indexOf("\n\n");
      }
      return out;
    },
  };
}

/** Reads a fetch response as server-sent events until the stream ends. */
export async function* readEvents(response: Response): AsyncGenerator<SseMessage> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = createSseParser();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      yield* parser.push(decoder.decode(value, { stream: true }));
    }
    yield* parser.push(decoder.decode() + "\n\n");
  } finally {
    reader.releaseLock();
  }
}
