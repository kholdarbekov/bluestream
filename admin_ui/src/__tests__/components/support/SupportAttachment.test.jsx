import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';

import SupportAttachment from '../../../components/support/SupportAttachment';
import adminService from '../../../services/adminService';

vi.mock('../../../services/adminService', () => ({
  __esModule: true,
  default: { getSupportAttachmentBlob: vi.fn() },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || (typeof opts === 'string' ? opts : key) }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  global.URL.createObjectURL = vi.fn(() => 'blob:fake-url');
  global.URL.revokeObjectURL = vi.fn();
});

it('fetches the photo through the proxy and renders it', async () => {
  adminService.getSupportAttachmentBlob.mockResolvedValue(new Blob(['x'], { type: 'image/jpeg' }));

  render(<SupportAttachment message={{ id: 7, message_type: 'photo', has_attachment: true, attachment_size: 1024 }} />);

  await waitFor(() => expect(adminService.getSupportAttachmentBlob).toHaveBeenCalledWith(7));
  // Match by accessible name, not bare role: antd's <Image> also renders a
  // preview-mask "eye" icon with role="img", so an unqualified getByRole('img')
  // is ambiguous. Click-to-zoom stays enabled — the fix is querying by name.
  await waitFor(() => expect(screen.getByRole('img', { name: /photo attachment/i })).toHaveAttribute('src', 'blob:fake-url'));
});

it('refuses to fetch an oversize attachment and says why', async () => {
  // Drives the backend-published field, not a locally re-derived threshold —
  // the 20 MB rule now has exactly one place deciding it (the backend).
  render(<SupportAttachment message={{
    id: 8, message_type: 'document', has_attachment: true, attachment_size: 25 * 1024 * 1024,
    attachment_too_large: true,
  }} />);

  expect(await screen.findByText(/too large/i)).toBeInTheDocument();
  expect(adminService.getSupportAttachmentBlob).not.toHaveBeenCalled();
});

it('trusts the backend field even when the raw size would say otherwise', async () => {
  // If the frontend ever re-derived the threshold from attachment_size this
  // would pass wrongly — the point of the fix is that it does not.
  render(<SupportAttachment message={{
    id: 10, message_type: 'document', has_attachment: true, attachment_size: 10,
    attachment_too_large: true,
  }} />);

  expect(await screen.findByText(/too large/i)).toBeInTheDocument();
  expect(adminService.getSupportAttachmentBlob).not.toHaveBeenCalled();
});

it('shows an unavailable notice when Telegram no longer has the file', async () => {
  adminService.getSupportAttachmentBlob.mockRejectedValue(new Error('404'));

  render(<SupportAttachment message={{ id: 9, message_type: 'photo', has_attachment: true, attachment_size: 10 }} />);

  expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
});
