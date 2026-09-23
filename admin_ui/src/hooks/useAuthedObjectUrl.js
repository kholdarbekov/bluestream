import { useEffect, useState } from 'react';

/**
 * Fetch an authenticated file as a blob and hand back a local object URL.
 *
 * An <img src> cannot carry the admin's credentials to a Telegram-proxy route, so the bytes are
 * fetched through the API client and shown from a blob URL — revoked on unmount or when `key`
 * changes. Shared by the support inbox and visit photos (D27). `fetchBlob` is read when `key` or
 * `enabled` changes, never on every render.
 */
const useAuthedObjectUrl = (key, fetchBlob, enabled = true) => {
  const [state, setState] = useState({ url: null, failed: false });

  useEffect(() => {
    if (!enabled) return undefined;
    let url = null;
    let cancelled = false;
    setState({ url: null, failed: false });
    fetchBlob()
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setState({ url, failed: false });
      })
      .catch(() => { if (!cancelled) setState({ url: null, failed: true }); });
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled]);

  return state;
};

export default useAuthedObjectUrl;
