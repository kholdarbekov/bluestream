/**
 * Manager penalty proposals (D-Q11, F9): docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md
 * §6.3, §5.3 and §10.6.
 *
 * Driven through the page a manager uses, with Task 12's real PenaltyFormModal in propose mode and
 * the real salesPayService: only the axios instance (services/api) is mocked. A manager never sees
 * an amount (C11): M1 publishes none, and the page and the propose form draw none.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { message } from 'antd';
import dayjs from 'dayjs';

import PenaltyProposals from '../../pages/PenaltyProposals';
import api from '../../services/api';
import { PAY_HANDLED_CODES } from '../../services/salesPayService';
import { formatDate, formatDateTimeShort } from '../../utils/dateUtils';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  getCookie: vi.fn(),
}));
const mockAuth = { hasPermission: vi.fn(() => false), getUserRole: vi.fn(() => 'manager'), user: { role: 'manager' } };
vi.mock('../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    i18n: { language: 'en' },
    // A refusal's copy is looked up by its key, so the key is echoed: it proves the inline Alert
    // is the seeded row, not the backend's raw sentence. Every other call reads back its English
    // fallback with i18next's `{{token}}` interpolation.
    t: (key, fallback, options) => {
      if (key.includes('.error.')) return key;
      const text = typeof fallback === 'string' ? fallback : (fallback?.defaultValue ?? key);
      const values = (typeof fallback === 'object' && fallback) || options || {};
      return text.replace(/\{\{(\w+)\}\}/g, (_, token) => (
        values[token] !== undefined ? String(values[token]) : `{{${token}}}`
      ));
    },
  }),
}));
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() },
  };
});

const TODAY = dayjs().format('YYYY-MM-DD');
const HANDLED = { handledErrorCodes: PAY_HANDLED_CODES };
const STATUSES = ['proposed', 'confirmed', 'rejected', 'cancelled'];

// Every key `serialize_penalty_proposal` publishes. test_admin_ui_payload_fixture_contracts.py
// holds this set to the backend's own pinned key set (PROPOSAL_KEYS): no amount key exists in it.
const PROPOSAL_ROW_KEYS = new Set([
  'id', 'agent', 'type', 'incident_date', 'reason', 'evidence', 'status', 'origin', 'proposed_by',
  'proposed_at', 'decided_at',
]);
const proposalRow = (overrides) => Object.assign(
  Object.fromEntries([...PROPOSAL_ROW_KEYS].map((key) => [key, null])),
  { origin: 'proposal' },
  overrides,
);

// M1 `types`: active types only, `{id, names}`, never a default amount.
const LATE = { id: 3, names: { en: 'Late for the route', uz: 'Marshrutga kechikish', ru: 'Опоздание на маршрут' } };
const NO_PHOTO = { id: 4, names: { en: 'No shelf photo', uz: "Tokcha surati yo'q", ru: 'Нет фото полки' } };

const PROPOSED = proposalRow({
  id: 31,
  agent: { user_id: 41, name: 'Aziz Karimov' },
  type: LATE,
  incident_date: '2026-10-12',
  reason: 'Started the route at 11:40',
  evidence: 'Route sheet and check-ins of the day',
  status: 'proposed',
  proposed_by: { id: 9, name: 'Mansur Manager' },
  proposed_at: '2026-10-12T12:00:00+00:00',
});
const CONFIRMED = proposalRow({
  id: 30,
  agent: { user_id: 52, name: 'Nodira Karimova' },
  type: NO_PHOTO,
  incident_date: '2026-10-10',
  reason: 'No shelf photo at three outlets',
  evidence: 'Visits 3301, 3302 and 3305',
  status: 'confirmed',
  proposed_by: { id: 9, name: 'Mansur Manager' },
  proposed_at: '2026-10-10T15:00:00+00:00',
  decided_at: '2026-10-11T06:30:00+00:00',
});
const AGENTS = [{ user_id: 41, full_name: 'Aziz Karimov' }, { user_id: 52, full_name: 'Nodira Karimova' }];

// M1's `data`: `meta` rides inside it, with the published statuses and the active types.
const m1Page = (items) => ({
  items,
  meta: { page: 1, per_page: 20, total: items.length, pages: 1, has_next: false, has_prev: false },
  statuses: STATUSES,
  types: [LATE, NO_PHOTO],
});

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
};

const mount = ({ page = m1Page([PROPOSED, CONFIRMED]), canManagePay = false } = {}) => {
  mockAuth.hasPermission.mockImplementation((permission) => (
    permission === 'can_manage_sales_pay' ? canManagePay : permission === 'can_review_agent_orders'
  ));
  api.get.mockImplementation(async (url) => {
    if (url === '/admin/sales/penalty-proposals') return { data: { success: true, data: page } };
    if (url === '/admin/staff/sales-agents') return { data: { data: { items: AGENTS }, meta: { total: AGENTS.length } } };
    throw new Error(`unexpected GET ${url}`);
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/sales/penalty-proposals']}>
        <Routes>
          <Route path="/sales/penalty-proposals" element={<PenaltyProposals />} />
          <Route path="/sales/compensation" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
};

const proposalReads = () => api.get.mock.calls.filter(([url]) => url === '/admin/sales/penalty-proposals');
const sentBody = (call = 0) => JSON.parse(JSON.stringify(api.post.mock.calls[call][1]));
// A form field of the propose modal, found by its label (the modal's inline defaults).
const fieldOf = (dialog, label) => within(dialog).getByText(label, { selector: 'label' }).closest('.ant-form-item');
const pick = async (container, title) => {
  fireEvent.mouseDown(container.querySelector('.ant-select-selector'));
  const option = () => document.querySelector(`.ant-select-dropdown .ant-select-item-option[title="${title}"]`);
  await waitFor(() => expect(option()).toBeTruthy());
  fireEvent.click(option());
};

// A regex, not the exact string: antd folds the button's PlusOutlined icon `aria-label="plus"`
// into its computed accessible name (the same reason SalesAgents.test.js matches its own
// icon+text "Add sales agent" button with a case-insensitive regex rather than an exact string).
const openProposeForm = async () => {
  fireEvent.click(await screen.findByRole('button', { name: /propose penalty/i }));
  return screen.findByRole('dialog');
};

// Agent, Type, Incident date (today, from the calendar), Reason, Evidence; then the modal's OK.
const fillAndSubmit = async (dialog) => {
  await pick(fieldOf(dialog, 'Agent'), 'Aziz Karimov');
  await pick(fieldOf(dialog, 'Type'), 'Late for the route');
  const dateInput = fieldOf(dialog, 'Incident date').querySelector('.ant-picker input');
  fireEvent.mouseDown(dateInput);
  fireEvent.click(dateInput);
  const cell = () => document.querySelector(`.ant-picker-dropdown td[title="${TODAY}"]`);
  await waitFor(() => expect(cell()).toBeTruthy());
  fireEvent.click(cell());
  fireEvent.change(fieldOf(dialog, 'Reason').querySelector('textarea, input'), {
    target: { value: 'Started the route at 11:40' },
  });
  fireEvent.change(fieldOf(dialog, 'Evidence').querySelector('textarea, input'), {
    target: { value: 'Route sheet and check-ins of the day' },
  });
  fireEvent.click(dialog.querySelector('.ant-modal-footer .ant-btn-primary'));
};

beforeEach(() => {
  vi.clearAllMocks();
});

it('lists the proposals with the eight manager columns and no amount anywhere', async () => {
  mount();
  const row = (await screen.findByText('Started the route at 11:40')).closest('tr');

  expect(proposalReads()[0][1]).toMatchObject({ params: { page: 1, per_page: 20 } });
  expect([...document.querySelectorAll('.ant-table-thead th')].map((th) => th.textContent)).toEqual([
    'Incident date', 'Agent', 'Type', 'Reason', 'Evidence', 'Proposed by', 'Status', 'Decided at',
  ]);
  expect(within(row).getByText(formatDate('2026-10-12'))).toBeInTheDocument();
  expect(within(row).getByText('Aziz Karimov')).toBeInTheDocument();
  expect(within(row).getByText('Late for the route')).toBeInTheDocument();
  expect(within(row).getByText('Route sheet and check-ins of the day')).toBeInTheDocument();
  expect(within(row).getByText('Mansur Manager')).toBeInTheDocument();
  expect(within(row).getByText('proposed')).toBeInTheDocument();
  expect(within(row).getByText('—')).toBeInTheDocument();
  const decided = screen.getByText('No shelf photo at three outlets').closest('tr');
  expect(within(decided).getByText(formatDateTimeShort('2026-10-11T06:30:00+00:00'))).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/amount|UZS/i);
});

it('filters by status and agent from page 1', async () => {
  mount();
  await screen.findByText('Started the route at 11:40');

  await pick(screen.getByTestId('penalty-proposals-status'), 'confirmed');
  await waitFor(() => expect(proposalReads().at(-1)[1].params).toMatchObject({ status: 'confirmed', page: 1 }));
  await pick(screen.getByTestId('penalty-proposals-agent'), 'Nodira Karimova');
  await waitFor(() => expect(proposalReads().at(-1)[1].params).toMatchObject({ status: 'confirmed', agent_id: 52, page: 1 }));
});

it('says so when there are no proposals', async () => {
  mount({ page: m1Page([]) });

  expect(await screen.findByText('No penalty proposals yet.')).toBeInTheDocument();
});

it('proposes with exactly the five fields, no amount, and the pay refusals named', async () => {
  mount();
  const dialog = await openProposeForm();
  expect(within(dialog).queryByText(/amount/i)).toBeNull();
  expect(dialog.querySelector('.ant-input-number')).toBeNull();
  const readsBefore = proposalReads().length;
  api.post.mockResolvedValue({ data: { success: true, data: { proposal: { ...PROPOSED, id: 32 } } } });

  await fillAndSubmit(dialog);

  await waitFor(() => expect(message.success).toHaveBeenCalledWith('Proposal sent to an administrator'));
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith(
    '/admin/sales/penalty-proposals',
    {
      agent_user_id: 41,
      penalty_type_id: 3,
      incident_date: TODAY,
      reason: 'Started the route at 11:40',
      evidence: 'Route sheet and check-ins of the day',
    },
    HANDLED,
  );
  expect(sentBody()).toStrictEqual({
    agent_user_id: 41,
    penalty_type_id: 3,
    incident_date: TODAY,
    reason: 'Started the route at 11:40',
    evidence: 'Route sheet and check-ins of the day',
  });
  // `['penaltyProposals']` is invalidated, so the new proposal shows without a reload.
  await waitFor(() => expect(proposalReads().length).toBeGreaterThan(readsBefore));
  expect(message.error).not.toHaveBeenCalled();
});

it('explains a 403 SALES_PAY_SELF_DECISION once, inline, with no toast', async () => {
  mount();
  const dialog = await openProposeForm();
  api.post.mockRejectedValue(Object.assign(new Error('Request failed with status code 403'), {
    response: {
      status: 403,
      data: { success: false, message: 'You cannot propose or decide anything about your own pay', error_code: 'SALES_PAY_SELF_DECISION' },
    },
  }));

  await fillAndSubmit(dialog);

  const alert = await within(dialog).findByRole('alert');
  expect(alert).toHaveTextContent('sales_agents:pay.error.sales_pay_self_decision');
  expect(dialog.querySelectorAll('.ant-alert-error')).toHaveLength(1);
  expect(message.error).not.toHaveBeenCalled();
  expect(message.success).not.toHaveBeenCalled();
  // The form stays open, so the manager reads why.
  expect(screen.getByRole('dialog')).toBe(dialog);
});

it('sends an administrator to the full Penalties tab and reads nothing here', async () => {
  mount({ canManagePay: true });

  await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/sales/compensation?tab=penalties'));
  expect(proposalReads()).toHaveLength(0);
});
