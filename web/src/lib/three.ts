let loaded: Promise<{
  THREE: typeof import("three");
  OrbitControls: typeof import("three/addons/controls/OrbitControls.js").OrbitControls;
}> | null = null;

/** three.js and its orbit controls, loaded the first time the fact map opens: a chunk of their
 * own, so the app's main bundle does not carry them. A failed load is forgotten so the next
 * open tries again. */
export function loadThree() {
  loaded ??= Promise.all([import("three"), import("three/addons/controls/OrbitControls.js")])
    .then(([THREE, controls]) => ({ THREE, OrbitControls: controls.OrbitControls }))
    .catch((error) => {
      loaded = null;
      throw error;
    });
  return loaded;
}
