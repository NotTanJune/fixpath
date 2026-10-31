/** A quote found in a text. Whitespace may differ between the quote and the text, since a
 * citation can carry line breaks that the document view shows as spaces. */
export function findQuote(text: string, quote: string): [number, number] | null {
  const direct = text.indexOf(quote);
  if (direct !== -1) return [direct, direct + quote.length];
  const words = quote
    .trim()
    .split(/\s+/)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!words.length || !words[0]) return null;
  const match = new RegExp(words.join("\\s+")).exec(text);
  return match ? [match.index, match.index + match[0].length] : null;
}
