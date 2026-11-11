import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import PlaceGroupConfirmModal from '../../components/PlaceGroupConfirmModal';
import { useAuthStore } from '../../stores/authStore';

vi.mock('../../services/adminService');

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => fallback || key }),
}));

const NOTE_PLACEHOLDER = 'Place note (optional)';

const open = (action, props = {}) => {
  const onConfirm = vi.fn();
  render(<PlaceGroupConfirmModal action={action} onConfirm={onConfirm} onCancel={vi.fn()} {...props} />);
  return onConfirm;
};

const ok = () => screen.getByRole('button', { name: /^OK$/ });
const note = () => screen.queryByPlaceholderText(NOTE_PLACEHOLDER);

describe('PlaceGroupConfirmModal place note and case-screen fields', () => {
  beforeEach(() => {
    useAuthStore.setState({ permissions: { can_manage_users: true } });
  });
  afterEach(() => useAuthStore.setState({ permissions: {} }));

  it('create with payload.notes shows the note pre-filled, with a 1000 limit', () => {
    open({ kind: 'create', payload: { addressIds: [1, 2], notes: 'Gate 12' } });
    expect(note()).toHaveValue('Gate 12');
    expect(note()).toHaveAttribute('maxlength', '1000');
  });

  it('create without payload.notes shows no note field', () => {
    open({ kind: 'create', payload: { addressIds: [1, 2] } });
    expect(note()).toBeNull();
  });

  it('create sends the typed note, and the pre-filled label and reason', async () => {
    const user = userEvent.setup();
    const onConfirm = open({
      kind: 'create',
      payload: { addressIds: [1, 2], notes: '', label: 'Office', reason: 'Driver says shared' },
    });
    expect(screen.getByPlaceholderText(/Label/)).toHaveValue('Office');
    expect(screen.getByPlaceholderText(/Reason/)).toHaveValue('Driver says shared');
    await user.type(note(), 'Back door');
    await user.click(ok());
    expect(onConfirm.mock.calls[0][0]).toMatchObject({
      addressIds: [1, 2],
      label: 'Office',
      reason: 'Driver says shared',
      notes: 'Back door',
    });
  });

  it('a modal that did not show the note never sends notes', async () => {
    const user = userEvent.setup();
    const onConfirm = open({ kind: 'create', payload: { addressIds: [1, 2] } });
    await user.type(screen.getByPlaceholderText(/Reason/), 'r');
    await user.click(ok());
    expect(Object.keys(onConfirm.mock.calls[0][0])).not.toContain('notes');
  });

  it('rename: an unchanged label alone keeps OK disabled; a changed note enables it', async () => {
    const user = userEvent.setup();
    const onConfirm = open({
      kind: 'rename',
      payload: { groupId: 5, currentLabel: 'Office', notes: '' },
    });
    await user.type(screen.getByPlaceholderText(/Reason/), 'why');
    expect(ok()).toBeDisabled();
    await user.type(note(), 'Back door');
    expect(ok()).toBeEnabled();
    await user.click(ok());
    expect(onConfirm.mock.calls[0][0]).toMatchObject({
      label: 'Office',
      reason: 'why',
      notes: 'Back door',
    });
  });

  it('rename of an unlabeled place: a note alone enables OK and sends no label', async () => {
    const user = userEvent.setup();
    const onConfirm = open({
      kind: 'rename',
      payload: { groupId: 5, currentLabel: '', notes: '' },
    });
    expect(screen.getByPlaceholderText(/Label/)).toHaveValue('');
    await user.type(screen.getByPlaceholderText(/Reason/), 'why');
    expect(ok()).toBeDisabled();
    await user.type(note(), 'Gate code 12');
    expect(ok()).toBeEnabled();
    await user.click(ok());
    expect(onConfirm.mock.calls[0][0]).toMatchObject({ label: null, reason: 'why', notes: 'Gate code 12' });
  });

  it('rename: clearing an existing label keeps OK disabled, even with a changed note', async () => {
    const user = userEvent.setup();
    open({ kind: 'rename', payload: { groupId: 5, currentLabel: 'Office', notes: '' } });
    await user.type(screen.getByPlaceholderText(/Reason/), 'why');
    await user.clear(screen.getByPlaceholderText(/Label/));
    await user.type(note(), 'Gate code 12');
    expect(ok()).toBeDisabled();
  });

  it('the place-note hint says who sees it: admins only', () => {
    open({ kind: 'rename', payload: { groupId: 5, currentLabel: 'Office', notes: '' } });
    expect(
      screen.getByText("Visible to admins on this place's card; drivers and customers never see it.")
    ).toBeInTheDocument();
  });

  it('rename without opting in to notes shows no note field', () => {
    open({ kind: 'rename', payload: { groupId: 5, currentLabel: 'Office' } });
    expect(note()).toBeNull();
  });

  it('add shows the label input only when payload.currentLabel is given', () => {
    const { unmount } = render(
      <PlaceGroupConfirmModal
        action={{ kind: 'add', payload: { groupId: 5, currentLabel: 'Office' } }}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    expect(screen.getByPlaceholderText(/Label/)).toHaveValue('Office');
    unmount();
    open({ kind: 'add', payload: { groupId: 5 } });
    expect(screen.queryByPlaceholderText(/Label/)).toBeNull();
  });

  it('OK is disabled without can_manage_users', async () => {
    useAuthStore.setState({ permissions: {} });
    const user = userEvent.setup();
    open({ kind: 'create', payload: { addressIds: [1, 2] } });
    await user.type(screen.getByPlaceholderText(/Reason/), 'r');
    expect(ok()).toBeDisabled();
  });
});
