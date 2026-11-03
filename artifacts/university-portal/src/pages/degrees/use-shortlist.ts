import { useCallback, useEffect, useState } from "react";
import { MAX_COMPARE, readShortlist, writeShortlist, type CourseResult, type ShortlistItem } from "./lib";

export function useShortlist() {
  const [items, setItems] = useState<ShortlistItem[]>(() => readShortlist());
  const [compareIds, setCompareIds] = useState<number[]>([]);

  useEffect(() => {
    const onStorage = () => setItems(readShortlist());
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const persist = useCallback((next: ShortlistItem[]) => { writeShortlist(next); setItems(next); }, []);

  const toggle = useCallback((c: CourseResult) => {
    const current = readShortlist();
    if (current.some((i) => i.id === c.id)) {
      persist(current.filter((i) => i.id !== c.id));
      setCompareIds((ids) => ids.filter((x) => x !== c.id));
    } else {
      persist([...current, { id: c.id, course_name: c.course_name, university: c.university.name }]);
    }
  }, [persist]);

  const remove = useCallback((id: number) => {
    persist(readShortlist().filter((i) => i.id !== id));
    setCompareIds((ids) => ids.filter((x) => x !== id));
  }, [persist]);

  const clear = useCallback(() => { persist([]); setCompareIds([]); }, [persist]);

  const toggleCompare = useCallback((id: number) => {
    setCompareIds((ids) => ids.includes(id) ? ids.filter((x) => x !== id) : ids.length >= MAX_COMPARE ? ids : [...ids, id]);
  }, []);

  const has = useCallback((id: number) => items.some((i) => i.id === id), [items]);
  return { items, has, toggle, remove, clear, compareIds, toggleCompare, setCompareIds };
}
