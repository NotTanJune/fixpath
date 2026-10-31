import { MAP_VIEW_KEY } from "./factMap";

export type FactsView = "map" | "list";

/** The viewer's Map / List choice; the map when nothing usable is stored or storage is blocked. */
export function readFactsView(storage: Pick<Storage, "getItem"> | null): FactsView {
  try {
    return storage?.getItem(MAP_VIEW_KEY) === "list" ? "list" : "map";
  } catch {
    return "map";
  }
}

export function writeFactsView(storage: Pick<Storage, "setItem"> | null, view: FactsView): void {
  try {
    storage?.setItem(MAP_VIEW_KEY, view);
  } catch {
    // a private window or blocked storage: the choice lasts until the page closes
  }
}

/** Why the list shows in place of the map: the browser has no WebGL, or the map or its code did
 * not load (an API that predates the map, a dropped connection). */
export type MapFailure = "webgl" | "load";

export function fallbackLine(reason: MapFailure): string {
  return reason === "webgl"
    ? "This browser cannot draw the 3D map, so the facts are listed."
    : "The map did not load, so the facts are listed.";
}
