// THE PLACE TO TUNE THE SPEED OF THE ANSWER REVEAL.
//
// Edit the numbers in PACE below: each is a time in milliseconds, and the reveal reads
// every one of them through paced(). To try a speed without editing anything, open the app
// with ?pace=<multiplier> (0.5 is twice as fast, 2 is twice as slow; the multiplier is held
// between 0.1 and 5, and 0 shows the final state at once), or run localStorage.setItem("pace", "0.5") in the console to keep it.
// Under prefers-reduced-motion, as with ?pace=0, the multiplier is 0: nothing waits and the final state shows
// at once. A live answer that arrives slower than the pace is never delayed further.

export const PACE = {
  /** How long each stage of the reveal holds before the next one starts. Each stage has a
   * narration line and lights its graph nodes. */
  stage: {
    /** The question read back. */
    question: 1400,
    /** The machine and the alarm. */
    machineAlarm: 1400,
    /** The root cause. */
    rootCause: 1400,
    /** The fix. */
    fix: 1400,
    /** The spare parts and the bin. */
    partsBin: 1400,
    /** The claims being checked against their sources. */
    claims: 1400,
  },
  /** A graph node snapping into place and taking its lit border. */
  nodeSnap: 500,
  /** A lit edge drawing from its source to its target. */
  edgeDraw: 700,
  /** The narration line fading in at the start of a stage. */
  narrationFade: 300,
  /** The live ingestion view. A recorded run waits `event` between one event and the next. */
  ingest: {
    /** The wait between two events of a recorded run. */
    event: 800,
    /** One chunk after another is marked on the document. */
    chunkMark: 300,
    /** A passage square flying in from the edge of the map to its place. */
    fly: 900,
    /** The next passage square setting off. */
    flyStagger: 150,
    /** A fact dot appearing, and the next one after it. */
    factPop: 260,
    /** An existing point flashing because the file matched it, and a square landing. */
    flash: 1100,
    /** A new node in the tree pulsing. */
    pulse: 1300,
    /** A live run stays on its end state this long before the new file opens. */
    settle: 1500,
  },
};

export const MIN_PACE = 0.1;
export const MAX_PACE = 5;

/** What paceFactor reads; a test passes its own, the app passes none. */
export interface PaceEnv {
  search?: string; // location.search
  hash?: string; // location.hash
  storage?: { getItem(key: string): string | null } | null;
  reduced?: boolean; // prefers-reduced-motion: reduce
}

function fromBrowser(): PaceEnv {
  const env: PaceEnv = {};
  try {
    env.search = window.location.search;
    env.hash = window.location.hash;
  } catch {
    // no location: the default pace
  }
  try {
    env.storage = window.localStorage;
  } catch {
    env.storage = null; // reading storage can throw where site data is blocked
  }
  try {
    env.reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    env.reduced = false;
  }
  return env;
}

/** A multiplier from text, held between MIN_PACE and MAX_PACE, except that exactly 0 stays 0 (the
 * reveal is off); null when it is not a number. */
function parse(text: string | null | undefined): number | null {
  if (text == null || text.trim() === "") return null;
  const value = Number(text);
  if (!Number.isFinite(value)) return null;
  if (value === 0) return 0;
  return Math.min(MAX_PACE, Math.max(MIN_PACE, value));
}

function fromUrl(env: PaceEnv): number | null {
  const query = (text: string) => parse(new URLSearchParams(text).get("pace"));
  const inHash = env.hash?.includes("?") ? env.hash.slice(env.hash.indexOf("?")) : "";
  return query(env.search ?? "") ?? query(inHash);
}

/** The multiplier for every duration in PACE: 0 under reduced motion, else ?pace= in the URL,
 * else localStorage "pace", else 1. */
export function paceFactor(env: PaceEnv = fromBrowser()): number {
  if (env.reduced) return 0;
  const url = fromUrl(env);
  if (url !== null) return url;
  try {
    return parse(env.storage?.getItem("pace")) ?? 1;
  } catch {
    return 1;
  }
}

/** A duration in ms at the current pace. */
export function paced(ms: number, env?: PaceEnv): number {
  return ms * paceFactor(env);
}
