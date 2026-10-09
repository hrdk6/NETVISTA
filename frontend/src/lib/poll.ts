import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/** GET `path` now and every `ms` (0 = once); `refresh()` re-fetches immediately. `key` forces a re-fetch when it changes. */
export function usePoll<T>(path: string | null, ms: number, key?: unknown): { data: T | null; refresh: () => void; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const fetchIt = useCallback(() => {
    if (!path) return;
    api
      .get<T>(path)
      .then((d) => {
        if (alive.current) {
          setData(d);
          setError(null);
        }
      })
      .catch((e) => alive.current && setError(e instanceof Error ? e.message : String(e)));
  }, [path]);
  useEffect(() => {
    alive.current = true;
    fetchIt();
    if (!ms) return () => void (alive.current = false);
    const id = setInterval(fetchIt, ms);
    return () => {
      alive.current = false;
      clearInterval(id);
    };
  }, [fetchIt, ms, key]);
  return { data, refresh: fetchIt, error };
}
