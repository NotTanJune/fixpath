// The words of the drop zone's refusals and of the line of files waiting. The steps of a run
// are in liveIngest.ts.

/** The line shown while other files are ahead of this one. */
export function queueLine(ahead: number): string {
  return ahead <= 0 ? "Waiting for your turn." : `Waiting for your turn. ${ahead} ${ahead === 1 ? "file" : "files"} ahead.`;
}

/** The server's refusal of an upload, in the words the view shows. The replay-mode refusal says
 * the server has no keys; any other reason is the server's own. */
export function refusalText(message: string): string {
  return message.startsWith("Ingest needs keys.")
    ? "This server runs without keys, so it cannot read new files."
    : message;
}
