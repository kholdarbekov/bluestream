import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import Login from '../../pages/Login';

// Stub the auth store: tests drive `login`'s mock and `isLoading` directly
// instead of reaching into Zustand internals. The store hook returns whatever
// we set it to per-test.
const mockLogin = vi.fn();
let mockIsLoading = false;

vi.mock('../../stores/authStore', () => ({
  useAuthStore: () => ({
    login: mockLogin,
    isLoading: mockIsLoading,
  }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback) => fallback || key,
  }),
}));

const renderLogin = (initialEntry = '/login') =>
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/dashboard" element={<div>dashboard-page</div>} />
        <Route path="/orders" element={<div>orders-page</div>} />
      </Routes>
    </MemoryRouter>,
  );

describe('Login page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockIsLoading = false;
  });

  it('submits credentials and navigates to /dashboard on success', async () => {
    mockLogin.mockResolvedValueOnce({ success: true });
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByPlaceholderText(/email_or_phone_placeholder/i), 'admin@example.com');
    await user.type(screen.getByPlaceholderText(/password_placeholder/i), 'pw-1234');
    await user.click(screen.getByRole('button', { name: /sign_in/i }));

    await waitFor(() => {
      expect(mockLogin).toHaveBeenCalledTimes(1);
    });
    expect(mockLogin).toHaveBeenCalledWith({
      email: 'admin@example.com',
      password: 'pw-1234',
    });

    await waitFor(() => {
      expect(screen.getByText('dashboard-page')).toBeInTheDocument();
    });
  });

  it('stays on /login when login() resolves with failure', async () => {
    mockLogin.mockResolvedValueOnce({ success: false, error: 'bad creds' });
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByPlaceholderText(/email_or_phone_placeholder/i), 'admin@example.com');
    await user.type(screen.getByPlaceholderText(/password_placeholder/i), 'wrong');
    await user.click(screen.getByRole('button', { name: /sign_in/i }));

    await waitFor(() => {
      expect(mockLogin).toHaveBeenCalledTimes(1);
    });
    // Still on login — no dashboard route rendered.
    expect(screen.queryByText('dashboard-page')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText(/email_or_phone_placeholder/i)).toBeInTheDocument();
  });

  it('blocks submission when required fields are empty', async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole('button', { name: /sign_in/i }));

    expect(mockLogin).not.toHaveBeenCalled();
    expect(await screen.findByText(/email_required/i)).toBeInTheDocument();
    expect(screen.getByText(/password_required/i)).toBeInTheDocument();
  });

  it('shows the loading spinner on the submit button when isLoading is true', () => {
    mockIsLoading = true;
    renderLogin();
    const btn = screen.getByRole('button', { name: /sign_in/i });
    // antd renders loading state by adding the `ant-btn-loading` modifier class.
    expect(btn).toHaveClass('ant-btn-loading');
  });
});
