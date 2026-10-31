import { useLayoutEffect, type RefObject } from "react";

/** Scrolls the viewer, and only the viewer, so that the first mark that carries `data-cite` is
 * in the middle of it. It runs when the viewed content changes, and again once the page has
 * settled (fonts loaded, scrollbars drawn), because both move the mark. */
export function useCenterOnCite(root: RefObject<HTMLElement | null>, content: unknown) {
  useLayoutEffect(() => {
    const box = root.current;
    if (!box) return;
    const center = () => {
      const mark = box.querySelector<HTMLElement>("[data-cite]");
      if (!mark) return;
      const b = box.getBoundingClientRect();
      const m = mark.getBoundingClientRect();
      box.scrollTop += m.top - b.top - (b.height - m.height) / 2;
      if (m.right > b.right || m.left < b.left) box.scrollLeft += m.left - b.left - 80;
    };
    center();
    const frame = requestAnimationFrame(center);
    let live = true;
    void document.fonts?.ready.then(() => live && center());
    return () => {
      live = false;
      cancelAnimationFrame(frame);
    };
  }, [root, content]);
}
