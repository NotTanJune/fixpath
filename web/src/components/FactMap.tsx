import { useEffect, useMemo, useRef } from "react";
import {
  LEAD,
  POINT_SIZE,
  centreName,
  clickAction,
  drawOrder,
  fitView,
  keyMove,
  lightUp,
  nearestPoint,
  turnAim,
  typeWords,
  type FactMapData,
  type Lit,
  type PointState,
  type Shown,
  type Vec3,
} from "../lib/factMap";
import { plainLabel } from "../lib/format";
import { sourcesOnce } from "../lib/api";
import { sourceLabel } from "../lib/sourceName";
import type { MapFailure } from "../lib/factsView";
import { reducedMotion } from "../lib/hooks";
import { loadThree } from "../lib/three";
import { runCaption, type MapRun } from "../lib/liveIngest";
import { planRun, spriteAt, visibleLines, type Planned, type Tone } from "../lib/mapRun";
import { PACE, paceFactor } from "../lib/pace";
import type { Span } from "../lib/types";

const TURN_MS = 600;
const RESUME_MS = 2000; // how long the map waits after a drag before it turns again
/** How close the camera may come, as a share of the distance of the home view. */
const CLOSEST = 0.02;
/** The wheel zoom: a notch of the wheel takes a third off the distance, so ten notches go from
 * home to the closest view. */
const ZOOM_SPEED = 7.6;
const DRAG_PX = 4;

const VERTEX = `
attribute vec3 fill;
attribute float size;
attribute float square;
attribute float ring;
varying vec3 vFill;
varying float vSquare;
varying float vRing;
void main() {
  vFill = fill;
  vSquare = square;
  vRing = ring;
  gl_PointSize = size;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const FRAGMENT = `
uniform vec3 ringColor;
varying vec3 vFill;
varying float vSquare;
varying float vRing;
void main() {
  vec2 p = gl_PointCoord * 2.0 - 1.0;
  float d = vSquare > 0.5 ? max(abs(p.x), abs(p.y)) : length(p);
  if (d > 1.0) discard;
  vec3 c = (vRing > 0.5 && d > 0.7) ? ringColor : vFill;
  gl_FragColor = vec4(c, 1.0);
}
`;

// the points that a file being added puts on the map: a square or a dot, or a ring that swells and fades
const RUN_VERTEX = `
attribute vec3 fill;
attribute float size;
attribute float alpha;
attribute float shape;
varying vec3 vFill;
varying float vAlpha;
varying float vShape;
void main() {
  vFill = fill;
  vAlpha = alpha;
  vShape = shape;
  gl_PointSize = size;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const RUN_FRAGMENT = `
varying vec3 vFill;
varying float vAlpha;
varying float vShape;
void main() {
  vec2 p = gl_PointCoord * 2.0 - 1.0;
  float d = vShape > 0.5 && vShape < 1.5 ? max(abs(p.x), abs(p.y)) : length(p);
  if (d > 1.0) discard;
  if (vShape > 1.5 && d < 0.78) discard;
  gl_FragColor = vec4(vFill, vAlpha);
}
`;
const SHAPE = { circle: 0, square: 1, ring: 2 } as const;

/** Which parts of a run the viewer has not seen yet: only those fly in and flash. */
interface RunNews {
  flash: boolean;
  placed: boolean;
}

/** What the effect below needs from the scene to light it again without rebuilding it. */
interface Scene {
  apply: (lit: Lit) => void;
  turnTo: (pointIndex: number) => void;
  setRun: (run: MapRun | null, news: RunNews) => void;
}

/** The 3D map of every fact and passage. The facts of the open file that the graph pane shows are
 * lit, and the pane's centre is yellow; the scene is built once and relit when the file, its spans
 * or what the pane shows change. Without WebGL it calls onFail. */
/** What the colours and shapes on the map mean, for the file that is open. */
function Legend({ facts, passages, centre }: { facts: number; passages: number; centre: string | null }) {
  return (
    <div className="fm-legend">
      <p className="fm-lead">{LEAD}</p>
      <ul>
        {facts > 0 && (
          <li>
            <span className="fm-key fact" aria-hidden="true" />
            This file's {facts} {facts === 1 ? "fact" : "facts"}
          </li>
        )}
        {centre !== null && (
          <li>
            <span className="fm-key centre" aria-hidden="true" />
            {centre}
          </li>
        )}
        <li>
          <span className="fm-key passage" aria-hidden="true" />
          {passages > 0 ? "Passages they came from" : "No passages: this file is a table or a log"}
        </li>
        <li>
          <span className="fm-key faint" aria-hidden="true" />
          Everything else in the graph
        </li>
      </ul>
    </div>
  );
}

export default function FactMap({
  data,
  docId,
  spans,
  shown,
  onPick,
  onOpen,
  onFail,
  autoFocus = false,
  run,
}: {
  data: FactMapData;
  docId: string;
  spans: Span[];
  /** What the graph pane draws, or null before it has anything. */
  shown: Shown | null;
  /** A click on a point of the open file: the document moves to the fact. */
  onPick: (index: number) => void;
  /** A click on a point of another file: that file opens, at the fact when there is one. */
  onOpen: (docId: string, factId: string | null) => void;
  onFail: (reason: MapFailure) => void;
  /** Take the focus once the canvas exists, unless the viewer has already moved it elsewhere. */
  autoFocus?: boolean;
  /** A file is being added: its new points fly onto the map and the points it matched flash. Null
   * before the run has anything for the map; left out when no file is being added. */
  run?: MapRun | null;
}) {
  const stage = useRef<HTMLDivElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const centreLabel = useRef<HTMLDivElement>(null);
  const scene = useRef<Scene | null>(null);

  const lit = useMemo(() => lightUp(data, docId, spans, shown), [data, docId, spans, shown]);
  const centreId = lit.centre < 0 ? null : (data.points[lit.centre]?.id ?? null);

  // the latest props, read by the scene's handlers without rebuilding it
  const latest = useRef({ lit, docId, spans, onPick, onOpen, onFail });
  latest.current = { lit, docId, spans, onPick, onOpen, onFail };

  // the plain names of the files, for the tip of a point that opens another file
  const fileNames = useRef(new Map<string, string>());
  useEffect(() => {
    sourcesOnce()
      .then((all) => {
        fileNames.current = new Map(all.map((s) => [s.id, sourceLabel(s).name]));
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    const host = stage.current;
    if (!host) return;
    let disposed = false;
    let cleanup = () => {};

    loadThree().then(
      ({ THREE, OrbitControls }) => {
        if (disposed) return;
        let renderer: InstanceType<typeof THREE.WebGLRenderer>;
        try {
          renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
          if (!renderer.getContext()) throw new Error("no context");
        } catch {
          latest.current.onFail("webgl");
          return;
        }
        const canvas = renderer.domElement;
        canvas.tabIndex = 0;
        canvas.setAttribute(
          "aria-label",
          "Fact map. Arrow keys turn it, plus and minus zoom, 0 resets. Every fact is also in the List view.",
        );
        host.prepend(canvas);
        if (autoFocus) {
          const now = document.activeElement;
          if (!now || now === document.body || now === host.closest('[role="dialog"]')) canvas.focus();
        }
        renderer.setClearColor(0x000000, 0);

        // colours from the page's tokens; the shader writes sRGB as it is
        const css = getComputedStyle(document.documentElement);
        const token = (name: string) => css.getPropertyValue(name).trim();
        const srgb = (value: string): [number, number, number] => {
          const out = { r: 0, g: 0, b: 0 };
          new THREE.Color(value).getRGB(out, THREE.SRGBColorSpace);
          return [out.r, out.g, out.b];
        };
        const FILL: Record<PointState, [number, number, number]> = {
          faint: srgb(token("--point-faint")),
          passage: srgb(token("--ink")),
          fact: srgb(token("--blue")),
          centre: srgb(token("--mark-solid")),
        };
        const blue = srgb(token("--blue"));

        const world = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(45, 1, 0.001, 100);

        // the camera stands back from the middle of the cloud so the cloud fills the view; the
        // fit is made again when the view's shape changes
        const aspectOf = () => Math.max(1, host.clientWidth) / Math.max(1, host.clientHeight);
        let fit = fitView(data.points, aspectOf());
        const target = new THREE.Vector3(...fit.target);
        // the viewer can come about 50 times closer than the home view, to tell near points apart
        const limits = () => ({ near: CLOSEST * fit.distance, far: 3 * fit.distance });
        const home = (): Vec3 => [target.x, target.y, target.z + fit.distance];
        camera.position.set(...home());
        camera.lookAt(target);

        const controls = new OrbitControls(camera, canvas);
        controls.target.copy(target);
        controls.enableDamping = true;
        controls.minDistance = limits().near;
        controls.maxDistance = limits().far;
        // the wheel and a pinch zoom toward the pointer, and a right drag or two fingers pan
        controls.zoomToCursor = true;
        controls.enablePan = true;
        controls.screenSpacePanning = true;
        controls.zoomSpeed = ZOOM_SPEED;
        // the point the camera looks at: the middle of the cloud at home, then wherever the viewer
        // has panned or zoomed to
        const pivot = controls.target;
        controls.autoRotate = !reducedMotion();
        controls.autoRotateSpeed = 0.6;
        controls.update();
        controls.saveState();
        // a drag stops the turning; letting go starts it again after a short pause
        let resume: ReturnType<typeof setTimeout> | undefined;
        const stopTurning = () => {
          clearTimeout(resume);
          controls.autoRotate = false;
        };
        const turnLater = () => {
          clearTimeout(resume);
          if (reducedMotion()) return;
          resume = setTimeout(() => {
            controls.autoRotate = true;
          }, RESUME_MS);
        };
        controls.addEventListener("start", stopTurning);
        controls.addEventListener("end", turnLater);

        const count = data.points.length;
        const geometry = new THREE.BufferGeometry();
        const attributes = {
          position: new THREE.BufferAttribute(new Float32Array(count * 3), 3),
          fill: new THREE.BufferAttribute(new Float32Array(count * 3), 3),
          size: new THREE.BufferAttribute(new Float32Array(count), 1),
          square: new THREE.BufferAttribute(new Float32Array(count), 1),
          ring: new THREE.BufferAttribute(new Float32Array(count), 1),
        };
        for (const [name, attribute] of Object.entries(attributes)) geometry.setAttribute(name, attribute);
        const material = new THREE.ShaderMaterial({
          vertexShader: VERTEX,
          fragmentShader: FRAGMENT,
          uniforms: { ringColor: { value: new THREE.Vector3(...blue) } },
          depthTest: false,
          // transparent puts the points in the same draw list as the lines, so renderOrder
          // (lines first) decides what is on top
          transparent: true,
        });
        const points = new THREE.Points(geometry, material);
        points.frustumCulled = false;
        points.renderOrder = 1;

        const lineMaterial = new THREE.LineBasicMaterial({
          color: new THREE.Color(token("--blue")),
          transparent: true,
          opacity: 0.45,
          depthTest: false,
        });
        let lines: InstanceType<typeof THREE.LineSegments> | null = null;
        world.add(points);

        // the points of a file being added, drawn over the map's own
        const tones: Record<Tone, [number, number, number]> = { ink: FILL.passage, blue: FILL.fact, mark: FILL.centre };
        const runMaterial = new THREE.ShaderMaterial({
          vertexShader: RUN_VERTEX,
          fragmentShader: RUN_FRAGMENT,
          depthTest: false,
          transparent: true,
        });
        const runGeometry = new THREE.BufferGeometry();
        let runCapacity = 0;
        const runPoints = new THREE.Points(runGeometry, runMaterial);
        runPoints.frustumCulled = false;
        runPoints.renderOrder = 2;
        runPoints.visible = false;
        world.add(runPoints);
        const runLineMaterial = new THREE.LineBasicMaterial({
          color: new THREE.Color(token("--blue")),
          transparent: true,
          opacity: 0.6,
          depthTest: false,
        });
        let runLines: InstanceType<typeof THREE.LineSegments> | null = null;
        let runLineCount = -1;
        const planned = new Map<string, Planned>();
        let runDirty = false; // the points are drawn once more after the plan is cleared, so that they go

        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        renderer.setPixelRatio(dpr);

        let states: PointState[] = [];
        let centreIndex = -1;
        let size = { w: 1, h: 1 };

        const apply = (next: Lit) => {
          states = next.states;
          const order = drawOrder(next.states);
          order.forEach((i, v) => {
            const point = data.points[i]!;
            const state = next.states[i]!;
            attributes.position.setXYZ(v, point.x, point.y, point.z);
            attributes.fill.setXYZ(v, ...FILL[state]);
            attributes.size.setX(v, POINT_SIZE[state] * dpr);
            attributes.square.setX(v, point.kind === "passage" ? 1 : 0);
            attributes.ring.setX(v, state === "centre" ? 1 : 0);
          });
          for (const attribute of Object.values(attributes)) attribute.needsUpdate = true;

          const segments = new Float32Array(next.lines.length * 6);
          next.lines.forEach(([a, b], k) => {
            const from = data.points[a]!;
            const to = data.points[b]!;
            segments.set([from.x, from.y, from.z, to.x, to.y, to.z], k * 6);
          });
          const lineGeometry = new THREE.BufferGeometry();
          lineGeometry.setAttribute("position", new THREE.BufferAttribute(segments, 3));
          if (lines) {
            world.remove(lines);
            lines.geometry.dispose();
          }
          lines = new THREE.LineSegments(lineGeometry, lineMaterial);
          lines.frustumCulled = false;
          lines.renderOrder = 0;
          world.add(lines);

          centreIndex = next.centre;
          const label = centreLabel.current;
          if (label) {
            label.textContent = centreIndex < 0 ? "" : plainLabel(data.points[centreIndex]!.label);
            label.hidden = centreIndex < 0;
          }
        };

        // the turn to the centre: the camera swings along the sphere to face it, and when the view
        // has been zoomed in somewhere else it looks at the centre instead
        let turn: { fromTarget: Vec3; toTarget: Vec3; fromOffset: Vec3; toOffset: Vec3; start: number } | null = null;
        const turnToward = (at: Vec3) => {
          // the pane moved to a new centre: the viewer is reading it, so the idle turn would only take it away
          stopTurning();
          const offset = camera.position.clone().sub(pivot);
          const aim = turnAim(at, pivot.toArray() as Vec3, offset.length());
          if (!aim) return;
          if (reducedMotion()) {
            turn = null;
            pivot.set(...aim.target);
            camera.position.set(...aim.target).add(new THREE.Vector3(...aim.offset));
            controls.update();
            return;
          }
          turn = {
            fromTarget: pivot.toArray() as Vec3,
            toTarget: aim.target,
            fromOffset: offset.toArray() as Vec3,
            toOffset: aim.offset,
            start: performance.now(),
          };
        };
        const turnTo = (i: number) => {
          const point = data.points[i];
          if (point) turnToward([point.x, point.y, point.z]);
        };
        controls.addEventListener("start", () => {
          turn = null;
        });

        // a run: its points are planned when they arrive, and drawn from the plan every frame
        const idAt = new Map(data.points.map((p) => [p.id, [p.x, p.y, p.z] as Vec3]));
        const cloudRadius = Math.max(0.5, ...data.points.map((p) => Math.hypot(p.x - fit.target[0], p.y - fit.target[1], p.z - fit.target[2])));
        const setRun = (run: MapRun | null, news: RunNews) => {
          if (!run) {
            planned.clear();
            runDirty = true;
            return;
          }
          const f = paceFactor();
          const t = PACE.ingest;
          const times = { fly: t.fly * f, flyStagger: t.flyStagger * f, factPop: t.factPop * f, flash: t.flash * f };
          const fresh = planRun(run, Object.fromEntries(idAt), performance.now(), times, { centre: fit.target, radius: cloudRadius }, news);
          let added = false;
          for (const p of fresh) {
            if (planned.has(p.key)) continue;
            planned.set(p.key, p);
            added ||= p.key.startsWith("p:") || p.key.startsWith("f:");
          }
          // the first points of a run turn the map to face them
          if (added && news.placed && run.passages.length + run.facts.length) {
            const all = [...run.passages, ...run.facts];
            const sum = all.reduce<Vec3>((a, p) => [a[0] + p.x, a[1] + p.y, a[2] + p.z], [0, 0, 0]);
            turnToward([sum[0] / all.length, sum[1] / all.length, sum[2] / all.length]);
          }
        };
        const drawRun = (now: number) => {
          const sprites = [...planned.values()].map((p) => spriteAt(p, now)).filter((x) => x !== null);
          runPoints.visible = sprites.length > 0;
          if (sprites.length) {
            if (sprites.length > runCapacity) {
              runCapacity = Math.max(32, sprites.length * 2);
              runGeometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(runCapacity * 3), 3));
              runGeometry.setAttribute("fill", new THREE.BufferAttribute(new Float32Array(runCapacity * 3), 3));
              runGeometry.setAttribute("size", new THREE.BufferAttribute(new Float32Array(runCapacity), 1));
              runGeometry.setAttribute("alpha", new THREE.BufferAttribute(new Float32Array(runCapacity), 1));
              runGeometry.setAttribute("shape", new THREE.BufferAttribute(new Float32Array(runCapacity), 1));
            }
            const at = (name: string) => runGeometry.getAttribute(name) as InstanceType<typeof THREE.BufferAttribute>;
            sprites.forEach((sp, i) => {
              at("position").setXYZ(i, sp.x, sp.y, sp.z);
              at("fill").setXYZ(i, ...tones[sp.tone]);
              at("size").setX(i, sp.size * dpr);
              at("alpha").setX(i, sp.alpha);
              at("shape").setX(i, SHAPE[sp.shape]);
            });
            for (const name of ["position", "fill", "size", "alpha", "shape"]) at(name).needsUpdate = true;
            runGeometry.setDrawRange(0, sprites.length);
          }
          const segments = visibleLines([...planned.values()], now);
          if (segments.length !== runLineCount) {
            runLineCount = segments.length;
            const array = new Float32Array(segments.length * 6);
            segments.forEach(([a, b], k) => array.set([...a, ...b], k * 6));
            const lineGeometry = new THREE.BufferGeometry();
            lineGeometry.setAttribute("position", new THREE.BufferAttribute(array, 3));
            if (runLines) {
              world.remove(runLines);
              runLines.geometry.dispose();
            }
            runLines = new THREE.LineSegments(lineGeometry, runLineMaterial);
            runLines.frustumCulled = false;
            runLines.renderOrder = 1;
            world.add(runLines);
          }
        };
        const stepTurn = (now: number) => {
          if (!turn) return;
          const t = Math.min(1, (now - turn.start) / TURN_MS);
          const eased = 1 - Math.pow(1 - t, 3);
          const a = new THREE.Vector3(...turn.fromOffset);
          const b = new THREE.Vector3(...turn.toOffset);
          const q = new THREE.Quaternion().slerp(
            new THREE.Quaternion().setFromUnitVectors(a.clone().normalize(), b.clone().normalize()),
            eased,
          );
          pivot.set(...turn.fromTarget).lerp(new THREE.Vector3(...turn.toTarget), eased);
          camera.position.copy(a).applyQuaternion(q).setLength(a.length()).add(pivot);
          if (t >= 1) turn = null;
        };

        // pointer: the tooltip, and a click on a point leads to its fact
        const projected = new THREE.Vector3();
        const screenOf = () =>
          data.points.map((p) => {
            projected.set(p.x, p.y, p.z).project(camera);
            if (projected.z > 1) return { x: Infinity, y: Infinity };
            return { x: ((projected.x + 1) / 2) * size.w, y: ((1 - projected.y) / 2) * size.h };
          });
        const at = (e: PointerEvent | MouseEvent) => {
          const box = canvas.getBoundingClientRect();
          return { x: e.clientX - box.left, y: e.clientY - box.top };
        };
        const showTip = (index: number | null, where: { x: number; y: number }) => {
          const el = tip.current;
          if (!el) return;
          if (index === null) {
            el.hidden = true;
            return;
          }
          const point = data.points[index]!;
          const text = el.querySelector<HTMLElement>(".fm-label");
          const type = el.querySelector<HTMLElement>(".fm-type");
          const file = el.querySelector<HTMLElement>(".fm-file");
          if (text) text.textContent = point.kind === "node" ? plainLabel(point.label) : point.label;
          if (type) type.textContent = typeWords(point);
          // a point that would open another file says which
          const now = latest.current;
          const action = clickAction(point, now.docId, now.spans, data);
          const name = action.kind === "open" ? fileNames.current.get(action.docId) : undefined;
          if (file) {
            file.textContent = name ? `In ${name}` : "";
            file.hidden = !name;
          }
          el.hidden = false;
          const left = Math.min(where.x + 14, Math.max(0, size.w - el.offsetWidth - 4));
          const top = Math.min(where.y + 14, Math.max(0, size.h - el.offsetHeight - 4));
          el.style.transform = `translate(${left}px, ${top}px)`;
        };
        let down: { x: number; y: number } | null = null;
        const onDown = (e: PointerEvent) => {
          down = { x: e.clientX, y: e.clientY };
          showTip(null, { x: 0, y: 0 });
        };
        const onMove = (e: PointerEvent) => {
          if (e.buttons) return;
          const where = at(e);
          showTip(nearestPoint(screenOf(), states, where), where);
        };
        const onLeave = () => showTip(null, { x: 0, y: 0 });
        const onClick = (e: MouseEvent) => {
          const moved = down ? Math.hypot(e.clientX - down.x, e.clientY - down.y) : 0;
          if (moved > DRAG_PX) return;
          const hit = nearestPoint(screenOf(), states, at(e));
          if (hit === null) return;
          const now = latest.current;
          const action = clickAction(data.points[hit]!, now.docId, now.spans, data);
          if (action.kind === "pick") now.onPick(action.index);
          else if (action.kind === "open") now.onOpen(action.docId, action.factId);
        };
        const onDouble = () => {
          turn = null;
          controls.reset();
        };
        const onKey = (e: KeyboardEvent) => {
          if (e.ctrlKey || e.metaKey || e.altKey) return;
          if (e.key === "0") {
            // home is the target as well as the distance, which a pan or a zoom to the pointer moved
            e.preventDefault();
            stopTurning();
            turn = null;
            controls.reset();
            return;
          }
          const from = camera.position.clone().sub(pivot).toArray() as Vec3;
          const next = keyMove(from, e.key, [0, 0, fit.distance], limits());
          if (!next) return;
          e.preventDefault();
          stopTurning();
          turn = null;
          camera.position.set(next[0] + pivot.x, next[1] + pivot.y, next[2] + pivot.z);
          controls.update();
        };
        canvas.addEventListener("pointerdown", onDown);
        canvas.addEventListener("pointermove", onMove);
        canvas.addEventListener("pointerleave", onLeave);
        canvas.addEventListener("click", onClick);
        canvas.addEventListener("dblclick", onDouble);
        canvas.addEventListener("keydown", onKey);

        const resize = () => {
          const w = Math.max(1, host.clientWidth);
          const h = Math.max(1, host.clientHeight);
          size = { w, h };
          renderer.setSize(w, h, false);
          camera.aspect = w / h;
          camera.updateProjectionMatrix();

          // the view changed shape: stand as far back as the new shape needs, in the same direction
          const next = fitView(data.points, w / h);
          const ratio = next.distance / fit.distance;
          fit = next;
          camera.position.sub(pivot).multiplyScalar(ratio).add(pivot);
          if (Math.abs(ratio - 1) > 1e-9) turn = null;
          controls.minDistance = limits().near;
          controls.maxDistance = limits().far;
          // double-click and reset() go to the home of the new shape
          controls.position0.set(...home());
          controls.target0.copy(target);
        };
        const observer = new ResizeObserver(resize);
        observer.observe(host);
        resize();

        const placeCentreLabel = () => {
          const label = centreLabel.current;
          if (!label || centreIndex < 0) return;
          const p = data.points[centreIndex]!;
          projected.set(p.x, p.y, p.z).project(camera);
          const x = ((projected.x + 1) / 2) * size.w + 12;
          const y = ((1 - projected.y) / 2) * size.h - label.offsetHeight / 2;
          const off = projected.z > 1 || x < 0 || y < 0 || x > size.w || y > size.h;
          label.style.visibility = off ? "hidden" : "visible";
          const left = Math.max(0, Math.min(x, size.w - label.offsetWidth - 4));
          label.style.transform = `translate(${left}px, ${Math.max(0, y)}px)`;
        };

        let last = performance.now();
        const frame = () => {
          const now = performance.now();
          stepTurn(now);
          if (planned.size || runLineCount > 0 || runDirty) {
            runDirty = false;
            drawRun(now);
          }
          controls.update((now - last) / 1000);
          last = now;
          renderer.render(world, camera);
          placeCentreLabel();
        };
        const start = () => {
          last = performance.now();
          renderer.setAnimationLoop(frame);
        };
        const onVisible = () => (document.hidden ? renderer.setAnimationLoop(null) : start());
        document.addEventListener("visibilitychange", onVisible);

        scene.current = { apply, turnTo, setRun };
        apply(latest.current.lit);
        syncRun.current();
        if (!document.hidden) start();

        cleanup = () => {
          renderer.setAnimationLoop(null);
          document.removeEventListener("visibilitychange", onVisible);
          observer.disconnect();
          canvas.removeEventListener("pointerdown", onDown);
          canvas.removeEventListener("pointermove", onMove);
          canvas.removeEventListener("pointerleave", onLeave);
          canvas.removeEventListener("click", onClick);
          canvas.removeEventListener("dblclick", onDouble);
          canvas.removeEventListener("keydown", onKey);
          clearTimeout(resume);
          controls.dispose();
          geometry.dispose();
          material.dispose();
          lines?.geometry.dispose();
          lineMaterial.dispose();
          runGeometry.dispose();
          runMaterial.dispose();
          runLines?.geometry.dispose();
          runLineMaterial.dispose();
          renderer.dispose();
          renderer.forceContextLoss();
          canvas.remove();
          scene.current = null;
        };
      },
      () => {
        if (!disposed) latest.current.onFail("load");
      },
    );

    return () => {
      disposed = true;
      cleanup();
    };
  }, [data]);

  // a different file, other spans or something else in the pane: light the same scene again
  useEffect(() => {
    scene.current?.apply(lit);
  }, [lit]);

  // a run: its points fly in and its matches flash, once each; a scene that is built again (the map
  // was read again after the load) shows them where they are
  const seen = useRef<{ key: number | null; flash: boolean; placed: boolean }>({ key: null, flash: false, placed: false });
  const syncRun = useRef(() => {});
  syncRun.current = () => {
    const sc = scene.current;
    if (!sc) return;
    if (!run) {
      seen.current = { key: null, flash: false, placed: false };
      sc.setRun(null, { flash: false, placed: false });
      return;
    }
    if (seen.current.key !== run.key) seen.current = { key: run.key, flash: false, placed: false };
    const news = {
      flash: run.flash.length > 0 && !seen.current.flash,
      placed: run.passages.length + run.facts.length > 0 && !seen.current.placed,
    };
    if (news.flash) seen.current.flash = true;
    if (news.placed) seen.current.placed = true;
    sc.setRun(run, news);
  };
  useEffect(() => syncRun.current(), [run]);

  // a new centre in the pane turns the map to face it; the first one leaves the idle turn alone
  const lastCentre = useRef<string | null>(null);
  useEffect(() => {
    const before = lastCentre.current;
    lastCentre.current = centreId;
    if (centreId === null || before === null || before === centreId) return;
    const index = data.points.findIndex((p) => p.id === centreId);
    if (index >= 0) scene.current?.turnTo(index);
  }, [centreId, data]);

  return (
    <div className="fact-map">
      <div ref={stage} className="fm-stage">
        <div ref={tip} className="fm-tip" hidden>
          <span className="fm-label" />
          <span className="fm-type" />
          <span className="fm-file" hidden />
        </div>
        <div ref={centreLabel} className="fm-open" hidden />
      </div>
      {run !== undefined ? (
        <p className="fm-caption">{runCaption(run)}</p>
      ) : (
        <Legend
          facts={lit.facts}
          passages={lit.passages}
          centre={lit.centre < 0 ? null : centreName(data.points[lit.centre]!)}
        />
      )}
      <p className="fm-note">
        <strong>Not every file is on the map.</strong> Only free-text passages are embedded:
        handover notes, maintenance notes and manuals. CSV files, logs and spreadsheets are read as
        rows, so they show as facts, not passages.
      </p>
    </div>
  );
}
