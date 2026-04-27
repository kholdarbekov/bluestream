import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from 'antd';

/**
 * Wraps Ant Design's <Button> with built-in pending-state handling.
 *
 * Pass an async (or promise-returning) `onClick` and the button will
 * render `loading` and stay disabled until the returned promise settles —
 * preventing double-submits without each page rolling its own state flag.
 *
 * If callers already manage their own loading flag (e.g. React Query
 * mutations), the explicit `loading` prop still wins.
 */
const AsyncButton = ({ onClick, loading, disabled, children, ...rest }) => {
  const [pending, setPending] = useState(false);
  const isMountedRef = useRef(true);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const handleClick = useCallback(
    (event) => {
      if (!onClick) return;
      const result = onClick(event);
      if (!result || typeof result.then !== 'function') {
        return;
      }
      setPending(true);
      const reset = () => {
        if (isMountedRef.current) setPending(false);
      };
      // Consume both branches so a caller's rejected promise doesn't surface as an
      // unhandled rejection. Caller-side error handling (e.g. mutation onError)
      // still runs because their handlers attached first.
      result.then(reset, reset);
    },
    [onClick],
  );

  const effectiveLoading = loading ?? pending;

  return (
    <Button
      {...rest}
      onClick={handleClick}
      loading={effectiveLoading}
      disabled={disabled || effectiveLoading}
    >
      {children}
    </Button>
  );
};

export default AsyncButton;
