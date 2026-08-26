import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import SupportComposer from '../../../components/support/SupportComposer';
import adminService from '../../../services/adminService';

vi.mock('../../../services/adminService', () => ({
  __esModule: true,
  default: {
    replySupportMessage: vi.fn(),
    sendSupportAttachment: vi.fn(),
    sendSupportLocation: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || (typeof opts === 'string' ? opts : key) }),
}));

beforeEach(() => vi.clearAllMocks());

it('sends typed text as a reply', async () => {
  adminService.replySupportMessage.mockResolvedValue({ data: { delivery: { success: true } } });

  render(<SupportComposer conversationId={31} onSent={vi.fn()} />);
  fireEvent.change(screen.getByPlaceholderText(/Type a message/i), { target: { value: 'on our way' } });
  fireEvent.click(screen.getByRole('button', { name: /Send/i }));

  // Assert the exact arguments: distinct typed values, so an argument swap
  // cannot pass.
  await waitFor(() => expect(adminService.replySupportMessage).toHaveBeenCalledWith(31, 'on our way'));
});

it('sends a chosen file with the typed text as its caption', async () => {
  adminService.sendSupportAttachment.mockResolvedValue({ data: { delivery: { success: true } } });
  const file = new File(['bytes'], 'diagram.jpg', { type: 'image/jpeg' });

  render(<SupportComposer conversationId={44} onSent={vi.fn()} />);
  fireEvent.change(screen.getByPlaceholderText(/Type a message/i), { target: { value: 'like this' } });
  fireEvent.change(screen.getByTestId('support-file-input'), { target: { files: [file] } });
  fireEvent.click(screen.getByRole('button', { name: /Send/i }));

  await waitFor(() => expect(adminService.sendSupportAttachment).toHaveBeenCalledWith(44, file, 'like this'));
  expect(adminService.replySupportMessage).not.toHaveBeenCalled();
});

it('guides the file picker toward the allowed extensions', () => {
  // FIX 4: cosmetic guidance only — the backend is the real gate — but it
  // should still steer admins away from disallowed types up front.
  //
  // Narrowed to the intersection valid in every environment: production's
  // ALLOWED_EXTENSIONS (business_app/config/production.py) is a strict
  // subset of base.py's, {"png","jpg","jpeg","pdf"}. Offering gif/doc/docx
  // here would invite a pick that production 400s.
  render(<SupportComposer conversationId={44} onSent={vi.fn()} />);
  expect(screen.getByTestId('support-file-input')).toHaveAttribute(
    'accept',
    '.png,.jpg,.jpeg,.pdf',
  );
});

it('sends a pin from pasted coordinates', async () => {
  adminService.sendSupportLocation.mockResolvedValue({ data: { delivery: { success: true } } });

  render(<SupportComposer conversationId={52} onSent={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: /pin/i }));
  fireEvent.change(screen.getByPlaceholderText(/41.32, 69.24/), { target: { value: '41.32354, 69.241036' } });
  fireEvent.click(screen.getByRole('button', { name: /Send pin/i }));

  await waitFor(() => expect(adminService.sendSupportLocation).toHaveBeenCalledWith(52, 41.32354, 69.241036));
});
