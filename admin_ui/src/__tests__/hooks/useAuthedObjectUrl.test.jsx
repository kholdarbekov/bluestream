import { act, renderHook, waitFor } from '@testing-library/react';
import { AxiosError, CanceledError } from 'axios';

import useAuthedObjectUrl from '../../hooks/useAuthedObjectUrl';

// A fetch that settles only when the test says so, and rejects the way axios does when its
// AbortSignal fires (a CanceledError), so the abort path is the real one.
const deferredFetch = () => {
  const calls = [];
  const fetchBlob = vi.fn((signal) => new Promise((resolve, reject) => {
    signal?.addEventListener('abort', () => reject(new CanceledError()));
    calls.push({ signal, resolve, reject });
  }));
  return { fetchBlob, calls };
};

const httpError = (status) => new AxiosError(
  `Request failed with status code ${status}`, AxiosError.ERR_BAD_REQUEST, {}, null,
  { status, statusText: '', data: new Blob(), headers: {}, config: {} },
);

beforeEach(() => {
  vi.clearAllMocks();
  let n = 0;
  global.URL.createObjectURL = vi.fn(() => `blob:url-${(n += 1)}`);
  global.URL.revokeObjectURL = vi.fn();
});

it('hands fetchBlob an AbortSignal and publishes the object URL', async () => {
  const { fetchBlob, calls } = deferredFetch();
  const { result } = renderHook(() => useAuthedObjectUrl(7, fetchBlob));

  expect(fetchBlob).toHaveBeenCalledTimes(1);
  expect(calls[0].signal).toBeInstanceOf(AbortSignal);
  expect(result.current).toMatchObject({ url: null, failed: false, errorStatus: null, loading: true });

  await act(async () => { calls[0].resolve(new Blob(['x'])); });

  expect(result.current).toMatchObject({ url: 'blob:url-1', failed: false, errorStatus: null, loading: false });
});

// Nothing renders after unmount, so only the abort itself is observable here; "an abort is not a
// failure" is pinned by the key-change case below, where the hook is still mounted.
it('aborts the in-flight download on unmount', () => {
  const { fetchBlob, calls } = deferredFetch();
  const { unmount } = renderHook(() => useAuthedObjectUrl(7, fetchBlob));
  expect(calls[0].signal.aborted).toBe(false);

  unmount();

  expect(calls[0].signal.aborted).toBe(true);
});

it('switching to another key aborts the old download; its abort is not a failure of the new one', async () => {
  const { fetchBlob, calls } = deferredFetch();
  const { result, rerender } = renderHook(({ id }) => useAuthedObjectUrl(id, fetchBlob), {
    initialProps: { id: 7 },
  });

  rerender({ id: 8 });
  await act(async () => {});

  expect(calls[0].signal.aborted).toBe(true);
  expect(calls[1].signal.aborted).toBe(false);
  expect(result.current).toMatchObject({ url: null, failed: false, errorStatus: null, loading: true });

  await act(async () => { calls[1].resolve(new Blob(['y'])); });
  expect(result.current).toMatchObject({ url: 'blob:url-1', failed: false });
});

it('publishes the HTTP status of a failure', async () => {
  const { fetchBlob, calls } = deferredFetch();
  const { result } = renderHook(() => useAuthedObjectUrl(7, fetchBlob));

  await act(async () => { calls[0].reject(httpError(404)); });

  expect(result.current).toMatchObject({ url: null, failed: true, errorStatus: 404, loading: false });
});

it('a failure with no response (timeout, network) has no status', async () => {
  const { fetchBlob, calls } = deferredFetch();
  const { result } = renderHook(() => useAuthedObjectUrl(7, fetchBlob));

  await act(async () => {
    calls[0].reject(new AxiosError('timeout of 60000ms exceeded', AxiosError.ECONNABORTED, {}));
  });

  expect(result.current).toMatchObject({ url: null, failed: true, errorStatus: null, loading: false });
});

it('retry() fetches again and clears the failure when it succeeds', async () => {
  const { fetchBlob, calls } = deferredFetch();
  const { result } = renderHook(() => useAuthedObjectUrl(7, fetchBlob));
  await act(async () => { calls[0].reject(new AxiosError('Network Error', AxiosError.ERR_NETWORK, {})); });
  expect(result.current.failed).toBe(true);

  act(() => { result.current.retry(); });

  expect(fetchBlob).toHaveBeenCalledTimes(2);
  expect(result.current).toMatchObject({ url: null, failed: false, errorStatus: null, loading: true });
  await act(async () => { calls[1].resolve(new Blob(['x'])); });
  expect(result.current).toMatchObject({ url: 'blob:url-1', failed: false, loading: false });
});

it('does not fetch while disabled, and fetches once enabled', async () => {
  const { fetchBlob } = deferredFetch();
  const { result, rerender } = renderHook(({ on }) => useAuthedObjectUrl(7, fetchBlob, on), {
    initialProps: { on: false },
  });

  expect(fetchBlob).not.toHaveBeenCalled();
  expect(result.current).toMatchObject({ url: null, failed: false, loading: false });

  rerender({ on: true });
  await waitFor(() => expect(fetchBlob).toHaveBeenCalledTimes(1));
});

it('still serves a caller that ignores the signal (visit photos)', async () => {
  const fetchBlob = vi.fn(() => Promise.resolve(new Blob(['x'])));
  const { result, unmount } = renderHook(() => useAuthedObjectUrl(41, () => fetchBlob(41)));

  await waitFor(() => expect(result.current.url).toBe('blob:url-1'));
  expect(fetchBlob).toHaveBeenCalledWith(41);

  unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:url-1');
});
