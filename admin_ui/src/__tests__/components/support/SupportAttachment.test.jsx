import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AxiosError, CanceledError } from 'axios';

import SupportAttachment from '../../../components/support/SupportAttachment';
import api from '../../../services/api';

// Only the wire is mocked: the real adminService.getSupportAttachmentBlob builds the request, so the
// assertions below pin the exact config the inbox sends.
vi.mock('../../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  getCookie: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => {
      const text = (opts && opts.defaultValue) || (typeof opts === 'string' ? opts : key);
      return typeof text === 'string' ? text.replace(/\{\{(\w+)\}\}/g, (_, name) => opts[name]) : text;
    },
  }),
}));

const URL_FOR = (id) => `/admin/support/messages/${id}/attachment`;
// The instance-wide axios timeout is 10 s; an 11 MB video over the Pi's uplink needs longer.
const BLOB_CONFIG = { responseType: 'blob', timeout: 60000, signal: expect.any(AbortSignal) };

const blobResponse = (type = 'video/mp4') => ({ data: new Blob(['x'], { type }) });
const httpError = (status) => new AxiosError(
  `Request failed with status code ${status}`, AxiosError.ERR_BAD_REQUEST, {}, null,
  { status, statusText: '', data: new Blob(), headers: {}, config: {} },
);
const timeoutError = () => new AxiosError('timeout of 60000ms exceeded', AxiosError.ECONNABORTED, {});

// The exact visible label. antd's icon adds its own aria-label ("play-circle") to the button's name.
const playButton = (label) => screen.getByText(label, { selector: 'button span' }).closest('button');

const video = (extra = {}) => ({
  id: 1001, message_type: 'video', has_attachment: true, attachment_size: 11 * 1024 * 1024,
  attachment_mime_type: 'video/mp4', attachment_too_large: false, ...extra,
});

beforeEach(() => {
  vi.clearAllMocks();
  global.URL.createObjectURL = vi.fn(() => 'blob:fake-url');
  global.URL.revokeObjectURL = vi.fn();
});

describe('photos and voice still load on their own', () => {
  it('fetches the photo through the proxy and renders it', async () => {
    api.get.mockResolvedValue(blobResponse('image/jpeg'));

    render(<SupportAttachment message={{ id: 7, message_type: 'photo', has_attachment: true, attachment_size: 1024 }} />);

    await waitFor(() => expect(api.get).toHaveBeenCalledWith(URL_FOR(7), BLOB_CONFIG));
    // Match by accessible name, not bare role: antd's <Image> also renders a
    // preview-mask "eye" icon with role="img", so an unqualified getByRole('img')
    // is ambiguous. Click-to-zoom stays enabled — the fix is querying by name.
    await waitFor(() => expect(screen.getByRole('img', { name: /photo attachment/i })).toHaveAttribute('src', 'blob:fake-url'));
  });

  it.each(['voice', 'audio'])('loads a %s message and plays it from the blob URL', async (messageType) => {
    api.get.mockResolvedValue(blobResponse('audio/ogg'));

    const { container } = render(
      <SupportAttachment message={{ id: 12, message_type: messageType, has_attachment: true, attachment_size: 8192 }} />,
    );

    await waitFor(() => expect(container.querySelector('audio')).toHaveAttribute('src', 'blob:fake-url'));
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(api.get).toHaveBeenCalledWith(URL_FOR(12), BLOB_CONFIG);
    expect(container.querySelector('audio')).toHaveAttribute('controls');
  });
});

describe('video downloads only when the admin asks to play it', () => {
  it.each([
    ['video', 11 * 1024 * 1024, 'Play video · 11 MB'],
    ['video_note', 850 * 1024, 'Play video · 850 KB'],
    // Just under 1 MiB rounds to 1024 KB, which is 1 MB, not "1024 KB".
    ['video_note', 1024 * 1024 - 1, 'Play video · 1 MB'],
    ['video', null, 'Play video'],
  ])('a %s of %s bytes shows "%s" and fetches nothing yet', async (messageType, size, label) => {
    render(<SupportAttachment message={video({ message_type: messageType, attachment_size: size })} />);

    expect(playButton(label)).toBeInTheDocument();
    await new Promise((r) => { setTimeout(r, 0); });
    expect(api.get).not.toHaveBeenCalled();
  });

  it('a click fetches with the 60 s timeout and an AbortSignal, then plays the blob', async () => {
    api.get.mockResolvedValue(blobResponse());
    const { container } = render(<SupportAttachment message={video()} />);

    fireEvent.click(playButton('Play video · 11 MB'));

    await waitFor(() => expect(container.querySelector('video')).toHaveAttribute('src', 'blob:fake-url'));
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(api.get).toHaveBeenCalledWith(URL_FOR(1001), BLOB_CONFIG);
    const player = container.querySelector('video');
    expect(player).toHaveAttribute('controls');
    expect(player).toHaveAttribute('preload', 'metadata');
    expect(player.autoplay).toBe(true);
  });

  it('the Play button spins while the video downloads, and leaving mid-download aborts it', async () => {
    api.get.mockReturnValue(new Promise(() => {}));
    const { unmount } = render(<SupportAttachment message={video()} />);
    fireEvent.click(playButton('Play video · 11 MB'));
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
    // A 20–60 s download needs visible feedback, or the admin keeps clicking.
    await waitFor(() => expect(playButton('Play video · 11 MB')).toHaveClass('ant-btn-loading'));
    const { signal } = api.get.mock.calls[0][1];
    expect(signal.aborted).toBe(false);

    unmount();

    expect(signal.aborted).toBe(true);
  });
});

describe('each failure says what happened', () => {
  it('a timeout says it could not load and Retry fetches again', async () => {
    api.get.mockRejectedValueOnce(timeoutError()).mockResolvedValueOnce(blobResponse());
    const { container } = render(<SupportAttachment message={video()} />);
    fireEvent.click(playButton('Play video · 11 MB'));

    expect(await screen.findByText("Couldn't load the attachment")).toBeInTheDocument();
    expect(screen.queryByText('Attachment is unavailable')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    await waitFor(() => expect(container.querySelector('video')).toHaveAttribute('src', 'blob:fake-url'));
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(api.get).toHaveBeenNthCalledWith(2, URL_FOR(1001), BLOB_CONFIG);
    expect(screen.queryByText("Couldn't load the attachment")).not.toBeInTheDocument();
  });

  it.each([
    ['a 5xx', () => httpError(502)],
    ['a network error', () => new AxiosError('Network Error', AxiosError.ERR_NETWORK, {})],
  ])('%s on a photo offers Retry too', async (_, makeError) => {
    api.get.mockRejectedValue(makeError());

    render(<SupportAttachment message={{ id: 9, message_type: 'photo', has_attachment: true, attachment_size: 10 }} />);

    expect(await screen.findByText("Couldn't load the attachment")).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('a 404 means Telegram no longer has the file', async () => {
    api.get.mockRejectedValue(httpError(404));

    render(<SupportAttachment message={{ id: 9, message_type: 'photo', has_attachment: true, attachment_size: 10 }} />);

    expect(await screen.findByText('Attachment is unavailable')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('a 413 from the server shows the too-large text', async () => {
    api.get.mockRejectedValue(httpError(413));

    render(<SupportAttachment message={{ id: 11, message_type: 'document', has_attachment: true }} />);

    expect(await screen.findByText(/too large/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('an abort is never shown as a failure', async () => {
    api.get.mockImplementation((_url, { signal }) => new Promise((_, reject) => {
      signal.addEventListener('abort', () => reject(new CanceledError()));
    }));
    const { rerender } = render(<SupportAttachment message={{ id: 21, message_type: 'voice', has_attachment: true }} />);
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));

    rerender(<SupportAttachment message={{ id: 22, message_type: 'voice', has_attachment: true }} />);

    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    expect(api.get.mock.calls[0][1].signal.aborted).toBe(true);
    expect(screen.queryByText("Couldn't load the attachment")).not.toBeInTheDocument();
    expect(screen.queryByText('Attachment is unavailable')).not.toBeInTheDocument();
  });

  it.each([
    ['video', video({ attachment_file_name: 'clip.mov', attachment_mime_type: 'video/quicktime' }), 'clip.mov'],
    ['video', video({ attachment_mime_type: 'video/quicktime' }), 'video-1001.mov'],
    ['voice', { id: 31, message_type: 'voice', has_attachment: true, attachment_mime_type: 'audio/ogg' }, 'voice-31.ogg'],
  ])('a %s the browser cannot decode offers the file as a download (%s)', async (kind, message, fileName) => {
    api.get.mockResolvedValue(blobResponse(message.attachment_mime_type));
    const { container } = render(<SupportAttachment message={message} />);
    if (kind === 'video') fireEvent.click(playButton(/^Play video/));
    const tag = kind === 'video' ? 'video' : 'audio';
    await waitFor(() => expect(container.querySelector(tag)).toHaveAttribute('src', 'blob:fake-url'));

    fireEvent.error(container.querySelector(tag));

    expect(await screen.findByText("This file can't play here")).toBeInTheDocument();
    expect(container.querySelector(tag)).not.toBeInTheDocument();
    const link = screen.getByRole('link', { name: /download/i });
    expect(link).toHaveAttribute('href', 'blob:fake-url');
    expect(link).toHaveAttribute('download', fileName);
  });
});

describe('the 20 MB rule stays the backend\'s', () => {
  it('refuses to fetch an oversize attachment and says why', async () => {
    // Drives the backend-published field, not a locally re-derived threshold —
    // the 20 MB rule now has exactly one place deciding it (the backend).
    render(<SupportAttachment message={{
      id: 8, message_type: 'document', has_attachment: true, attachment_size: 25 * 1024 * 1024,
      attachment_too_large: true,
    }} />);

    expect(await screen.findByText(/too large/i)).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it('trusts the backend field even when the raw size would say otherwise', async () => {
    // If the frontend ever re-derived the threshold from attachment_size this
    // would pass wrongly — the point of the fix is that it does not.
    render(<SupportAttachment message={{
      id: 10, message_type: 'document', has_attachment: true, attachment_size: 10,
      attachment_too_large: true,
    }} />);

    expect(await screen.findByText(/too large/i)).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it('an oversize video shows the too-large text, not a Play button', async () => {
    render(<SupportAttachment message={video({ attachment_size: 25 * 1024 * 1024, attachment_too_large: true })} />);

    expect(await screen.findByText(/too large/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /play video/i })).not.toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });
});
