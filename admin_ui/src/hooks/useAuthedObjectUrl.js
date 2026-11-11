import { useCallback, useEffect, useState } from 'react';

const IDLE = { url: null, failed: false, errorStatus: null, loading: false };

/**
 * Fetch an authenticated file as a blob and hand back a local object URL.
 *
 * An <img src> cannot carry the admin's credentials to a Telegram-proxy route, so the bytes are
 * fetched through the API client and shown from a blob URL — revoked on unmount or when `key`
 * changes. Shared by the support inbox and visit photos (D27). `fetchBlob` is read when `key` or
 * `enabled` changes, never on every render.
 *
 * `fetchBlob(signal)` receives an AbortSignal that fires when the component unmounts or `key`
 * changes, so a large download the admin walked away from stops instead of finishing for nobody.
 * A caller may ignore it. An abort is never reported as a failure. `errorStatus` is the HTTP status
 * of a failure (null for a timeout or a network error); `retry()` fetches again.
 */
const useAuthedObjectUrl = (key, fetchBlob, enabled = true) => {
  const [state, setState] = useState(IDLE);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    let url = null;
    let cancelled = false;
    setState({ ...IDLE, loading: true });
    fetchBlob(controller.signal)
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setState({ ...IDLE, url });
      })
      .catch((error) => {
        if (cancelled) return;
        setState({ ...IDLE, failed: true, errorStatus: error?.response?.status ?? null });
      });
    return () => {
      cancelled = true;
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  return { ...state, retry };
};

export default useAuthedObjectUrl;
