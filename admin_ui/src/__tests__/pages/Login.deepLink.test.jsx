import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';

import Login from '../../pages/Login';
import ProtectedRoute from '../../components/common/ProtectedRoute';

// One auth state for both components: logged out until `login` succeeds.
const mockAuth = {
  isAuthenticated: false,
  isLoading: false,
  initialize: vi.fn().mockResolvedValue(undefined),
  hasPermission: vi.fn(() => true),
  getUserRole: vi.fn(() => 'admin'),
  login: vi.fn(async () => {
    mockAuth.isAuthenticated = true;
    return { success: true };
  }),
};

vi.mock('../../stores/authStore', () => ({
  useAuthStore: () => mockAuth,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => fallback || key }),
}));

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}${location.hash}`}</div>;
};

const renderAt = (entry) =>
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/dashboard" element={<div>dashboard-page</div>} />
        <Route
          path="/users"
          element={(
            <ProtectedRoute>
              <LocationProbe />
            </ProtectedRoute>
          )}
        />
      </Routes>
    </MemoryRouter>
  );

const signIn = async (user) => {
  await user.type(await screen.findByPlaceholderText(/email_or_phone_placeholder/i), 'admin@example.com');
  await user.type(screen.getByPlaceholderText(/password_placeholder/i), 'pw-1234');
  await user.click(screen.getByRole('button', { name: /sign_in/i }));
};

describe('Login after a redirect', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockAuth.isAuthenticated = false;
  });

  it('keeps the Slack link\'s query: /users?tab=driver-reports&case=17 survives the login', async () => {
    const user = userEvent.setup();
    renderAt('/users?tab=driver-reports&case=17');

    await signIn(user);

    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/users?tab=driver-reports&case=17')
    );
    expect(mockAuth.login).toHaveBeenCalledTimes(1);
  });

  it('keeps the hash as well', async () => {
    const user = userEvent.setup();
    renderAt('/users?tab=driver-reports#case');

    await signIn(user);

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/users?tab=driver-reports#case'));
  });

  it('a direct visit to /login still lands on /dashboard', async () => {
    const user = userEvent.setup();
    renderAt('/login');

    await signIn(user);

    expect(await screen.findByText('dashboard-page')).toBeInTheDocument();
  });
});
