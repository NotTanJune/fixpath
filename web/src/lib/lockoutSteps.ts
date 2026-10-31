/** The two sentences that close every lockout text, kept together as the last step. */
const CLOSING = ["Apply lockout and tagout before starting.", "Verify zero energy."];

function squash(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/**
 * The safety text of an answer as short steps. The text is split into its sentences and
 * nothing in it is reworded: an exact repeat is dropped (the first one stays where it is), the
 * order is kept, and the closing "Apply lockout and tagout before starting. Verify zero
 * energy." is the last step, once. A text without that closing is not given one.
 */
export function lockoutSteps(text: string): string[] {
  const sentences = squash(text)
    .split(/(?<=[.!?])\s+/)
    .filter(Boolean);
  const closed =
    sentences.length >= 2 &&
    sentences.at(-2) === CLOSING[0] &&
    sentences.at(-1) === CLOSING[1];
  const body = closed ? sentences.slice(0, -2) : sentences;
  const steps: string[] = [];
  for (const sentence of body) {
    if (closed && CLOSING.includes(sentence)) continue;
    if (!steps.includes(sentence)) steps.push(sentence);
  }
  return closed ? [...steps, CLOSING.join(" ")] : steps;
}

/** Whether the answer owes the lockout band: the safety block says so, or a fix that is shown
 * needs lockout. The band's own steps come from the safety text (see lockoutSteps). */
export function needsBand(card: {
  safety: { required: boolean };
  fix: { requires_loto: boolean } | null;
  other_fixes: { requires_loto: boolean }[];
}): boolean {
  return card.safety.required || !!card.fix?.requires_loto || card.other_fixes.some((f) => f.requires_loto);
}

/** The steps of the lockout band of a card: its own text split into steps, or, when the text is
 * missing, the closing step alone, so that a band that is owed is never empty. */
export function bandSteps(card: { safety: { text: string } }): string[] {
  const steps = lockoutSteps(card.safety.text);
  return steps.length ? steps : [CLOSING.join(" ")];
}

/** What a lockout block is for when it is not one machine's own. A block with no procedure and no
 * manual page behind it is the generic one (an unknown line, a question that names no machine):
 * it still shows, and says plainly that it holds before work on any machine of the plant. The
 * block of a real answer, built from its machine's manual, has no such line. */
export function lockoutScope(safety: { procedure_ids: string[]; citations: unknown[] }): string | null {
  return safety.procedure_ids.length === 0 && safety.citations.length === 0
    ? "Before work on any Line 3 machine"
    : null;
}
