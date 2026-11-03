import { useEffect } from "react";

function upsertMeta(attr: "name" | "property", key: string, content: string) {
  let el = document.head.querySelector<HTMLMetaElement>(`meta[${attr}="${key}"]`);
  const created = !el;
  const previous = el?.getAttribute("content") ?? null;
  if (!el) { el = document.createElement("meta"); el.setAttribute(attr, key); document.head.appendChild(el); }
  el.setAttribute("content", content);
  return () => {
    if (created) el?.remove();
    else if (previous != null) el?.setAttribute("content", previous);
  };
}

export function usePageMeta(title: string, description: string) {
  useEffect(() => {
    const prevTitle = document.title;
    document.title = title;
    const undo = [
      upsertMeta("name", "description", description),
      upsertMeta("property", "og:title", title),
      upsertMeta("property", "og:description", description),
      upsertMeta("property", "og:type", "website"),
    ];
    return () => { document.title = prevTitle; undo.forEach((u) => u()); };
  }, [title, description]);
}
