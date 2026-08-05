import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';

import PlaceGroupPanel from './PlaceGroupPanel';
import adminService from '../services/adminService';

vi.mock('../services/adminService');

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => fallback || key }),
}));

// PlaceGroupPanel calls message.success/error in every mutation callback;
// unmocked antd v5 static `message` mounts a real portal outside act() and
// leaks state across tests. Same block as LinkedAccountsPanel.test.jsx.
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    message: {
      success: vi.fn(),
      error: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
      loading: vi.fn(),
      destroy: vi.fn(),
      open: vi.fn(),
    },
  };
});

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

const USER = { id: 11, first_name: 'Alice', last_name: 'Tester', phone: '+998900000001' };

// ---------------------------------------------------------------------------
// Payload contract — GET /admin/place-groups/<id>
// ---------------------------------------------------------------------------
// Key sets taken from the backend:
//   business_app/api/admin.py                       get_place_group_detail (route)
//   business_app/services/customer_link_service.py  get_place_group_detail
//                                                   get_place_group_events
//
// This file was the blind spot the place re-key slipped through: it fabricated
// `place_union_balance` and a per-member `balance`, neither of which the API has
// ever emitted, and asserted the balance LABEL without ever asserting a value.
// Validating fixtures against the real key set is what stops that recurring —
// a fabricated key now fails loudly instead of teaching the panel to read a
// field that is always undefined.
//
// Two mechanisms, because rejecting EXTRA keys alone is not enough:
//   1. `assertKeysMatch` below is bidirectional — a fixture that DROPS a key the
//      backend always sends fails too, so the fixture cannot drift from the set.
//   2. The sets themselves are hand-copied from the backend and would go stale
//      together with the fixture on the next rename, which would take this file
//      straight back to green-while-broken. They are therefore pinned against the
//      LIVE payload by `tests/unit/test_admin_ui_payload_fixture_contracts.py`,
//      which parses the sets out of this file and diffs them against the real
//      `GET /admin/place-groups/<id>` response. Rename `place_balance` in the
//      backend and that guard goes red, naming this file.
const GROUP_DETAIL_KEYS = new Set([
  'place_group_id',
  'label',
  'place_balance',
  'members',
  // Opaque on purpose: `cod` is CashCollectionService.get_place_cod_statement,
  // the money axis, and is not re-keyed by this plan.
  'cod',
  'events',
]);

// Members deliberately carry NO balance: a place holds ONE pool and it cannot
// be sliced per coworker (spec decision 4, quoted in get_place_group_detail).
// `suggested_bottles_leaving` is NOT that balance sneaking back in — it is the
// remove dialog's pre-fill for `bottles_leaving` (spec 7.1), a number nobody
// holds, derived from the member's own attributed ledger entries and clamped to
// what the place actually has.
const GROUP_MEMBER_KEYS = new Set([
  'address_id',
  'address_title',
  'full_address',
  'suggested_bottles_leaving',
  'owner',
]);

const GROUP_EVENT_KEYS = new Set([
  'id',
  'event_type',
  'acting_admin_id',
  'member_user_ids',
  'reason',
  'created_at',
]);

// ---------------------------------------------------------------------------
// Payload contract — GET /admin/place-groups/merge-preview
// ---------------------------------------------------------------------------
// Key sets taken from the backend:
//   business_app/api/admin.py                          get_place_group_merge_preview
//   business_app/services/bottle_tracking_service.py   build_merge_preview
//
// Deliberately NOT bidirectional, unlike the detail sets above: the route
// spreads the whole `serialize_bottle_ledger_entry` output onto every row, so a
// fixture forced to carry all of it would be mostly noise the panel never reads.
// These are the keys the panel INDEXES BY — a fixture missing one of them would
// let the panel read `undefined` and stay green. The other direction (the
// backend renaming one of these) is pinned against the LIVE payload by
// `tests/unit/test_admin_ui_payload_fixture_contracts.py`, which parses these
// two sets out of this file and asserts they are still a subset of the real
// `GET /admin/place-groups/merge-preview` response.
const MERGE_PREVIEW_KEYS = new Set([
  'entries',
  // Handed straight back as `previewEntryIds` so a merge decided against a
  // moved ledger is rejected (MERGE_PREVIEW_STALE) instead of silently wrong.
  'entry_ids',
  'computed_balance',
  'excluded_total',
  'resulting_balance',
  // The drift trio. `resulting_balance` is ledger-derived; on a place whose
  // stored figure the ledger never explained (dev address 24: stored 20.00,
  // zero ledger rows) it is NOT what the place will hold — that is
  // `projected_place_balance`. Rendering the first without the second is how a
  // dialog tells an admin "resulting balance 0" about a place holding 20.
  'stored_balance',
  'drift',
  'projected_place_balance',
]);

const MERGE_PREVIEW_ENTRY_KEYS = new Set([
  'id',
  'occurred_at',
  'event_type',
  'quantity',
  // The MERGED running total, attached transiently by build_merge_preview.
  // `balance_after` is the pre-merge column and would render the wrong story.
  'preview_balance_after',
  'user_name',
]);

const assertReadKeysPresent = (obj, required, what) => {
  const missing = [...required].filter((key) => !(key in obj));
  if (missing.length) {
    throw new Error(
      `${what} fixture is missing keys the panel reads: ${missing.join(', ')}. ` +
      'See get_place_group_merge_preview in business_app/api/admin.py.'
    );
  }
  return obj;
};

const mergePreview = (preview) => {
  assertReadKeysPresent(preview, MERGE_PREVIEW_KEYS, 'Merge preview');
  (preview.entries || []).forEach((e) =>
    assertReadKeysPresent(e, MERGE_PREVIEW_ENTRY_KEYS, 'Merge preview entry')
  );
  return preview;
};

/** A clean place: stored 7.00, ledger 4 + 3 = 7.00, drift 0 (dev group 9). */
const CLEAN_PREVIEW = mergePreview({
  entries: [
    {
      id: 41,
      quantity: 4,
      event_type: 'admin_adjustment',
      occurred_at: '2026-07-01T10:00:00+00:00',
      user_name: 'Alice Tester',
      preview_balance_after: 4,
      excluded: false,
    },
    {
      id: 42,
      quantity: 3,
      event_type: 'delivery',
      occurred_at: '2026-07-02T10:00:00+00:00',
      user_name: 'Carol Neighbor',
      preview_balance_after: 7,
      excluded: false,
    },
  ],
  entry_ids: [41, 42],
  computed_balance: 7,
  stored_balance: 7,
  drift: 0,
  excluded_total: 0,
  resulting_balance: 7,
  projected_place_balance: 7,
});

const assertKeysMatch = (obj, allowed, what) => {
  const fabricated = Object.keys(obj).filter((key) => !allowed.has(key));
  if (fabricated.length) {
    throw new Error(
      `${what} fixture invents keys the backend never emits: ${fabricated.join(', ')}. ` +
      'See get_place_group_detail in business_app/services/customer_link_service.py.'
    );
  }
  // The half that was missing. A rename lands as "the old key is gone", not as
  // "a new key appeared", so a validator that only rejects extras is blind to
  // exactly the change it exists to catch.
  const missing = [...allowed].filter((key) => !(key in obj));
  if (missing.length) {
    throw new Error(
      `${what} fixture is missing keys the backend always emits: ${missing.join(', ')}. ` +
      'See get_place_group_detail in business_app/services/customer_link_service.py.'
    );
  }
};

const placeGroupDetail = (detail) => {
  assertKeysMatch(detail, GROUP_DETAIL_KEYS, 'Place-group detail');
  (detail.members || []).forEach((m) => assertKeysMatch(m, GROUP_MEMBER_KEYS, 'Place-group member'));
  (detail.events || []).forEach((e) => assertKeysMatch(e, GROUP_EVENT_KEYS, 'Place-group event'));
  return detail;
};

/**
 * antd renders Select options into a body-level portal. Scoping to the
 * dropdown keeps the query unambiguous: the same person's name is also
 * printed in the group-member list behind the modal.
 */
const pickOption = async (user, labelPattern) => {
  const dropdown = await waitFor(() => {
    const element = document.querySelector('.ant-select-dropdown');
    expect(element).not.toBeNull();
    return element;
  });
  await user.click(await within(dropdown).findByText(labelPattern));
};

const openModalAndConfirm = async (user, reason) => {
  const reasonInput = await screen.findByPlaceholderText(/Reason/);
  await user.type(reasonInput, reason);
  await user.click(screen.getByRole('button', { name: /^OK$/ }));
};

/**
 * The merge review opens a SECOND modal on top of the create/add one, so both
 * an `OK` and a `Cancel` exist twice while it is open. Scope every query to the
 * modal under test rather than picking whichever button the DOM happens to
 * return first.
 */
const modalByTitle = async (titlePattern) => {
  const title = await screen.findByText(titlePattern);
  const modal = title.closest('.ant-modal');
  expect(modal).not.toBeNull();
  return modal;
};

const openMergeReview = async (user) => {
  await user.click(screen.getByRole('button', { name: /Review bottle history/ }));
  return modalByTitle(/Review the merged bottle history/);
};

const setNumber = async (user, input, value) => {
  await user.clear(input);
  await user.type(input, value);
};

/**
 * The suggestion list is OPT-IN: `get_place_group_suggestions` clusters the
 * FULL ungrouped estate per call and cannot be narrowed (a bounding box would
 * truncate a transitive component and void dismissals — plan E19), so the user
 * drawer must not bill that pass on every open. Every suggestion-driven flow
 * therefore starts by asking for the scan.
 */
const revealSuggestions = async (user) => {
  await user.click(
    await screen.findByRole('button', { name: /Find possible same-place matches/ })
  );
};

const clickSuggestionAction = async (user, actionPattern) => {
  await revealSuggestions(user);
  await user.click(await screen.findByRole('button', { name: actionPattern }));
};

describe('PlaceGroupPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    adminService.getUserAddresses.mockResolvedValue({
      data: {
        addresses: [
          { id: 1, title: 'Office', full_address: 'Office st 1', address_group_id: 7 },
          { id: 2, title: 'Home', full_address: 'Home st 2', address_group_id: null },
        ],
      },
    });

    adminService.getPlaceGroup.mockResolvedValue({
      data: placeGroupDetail({
        place_group_id: 7,
        label: 'Acme office',
        place_balance: 5,
        members: [
          {
            address_id: 1,
            address_title: 'Office',
            full_address: 'Office st 1',
            suggested_bottles_leaving: 2,
            owner: { id: 11, first_name: 'Alice', last_name: 'Tester', phone: '+998900000001' },
          },
          {
            address_id: 3,
            address_title: 'Office',
            full_address: 'Office st 1',
            suggested_bottles_leaving: 3,
            owner: { id: 22, first_name: 'Bob', last_name: 'Coworker', phone: '+998900000002' },
          },
        ],
        cod: { total_outstanding_amount: 35000, active_cod_debt_count: 2, items: [] },
        events: [],
      }),
    });

    adminService.getPlaceGroupSuggestions.mockResolvedValue({
      data: {
        suggestions: [
          {
            address_ids: [2, 9],
            distinct_customer_count: 2,
            signal_fingerprint: 'abc',
            members: [
              {
                address_id: 2,
                user_id: 11,
                first_name: 'Alice',
                last_name: 'Tester',
                phone: '+998900000001',
                title: 'Home',
                full_address: 'Home st 2',
              },
              {
                address_id: 9,
                user_id: 33,
                first_name: 'Carol',
                last_name: 'Neighbor',
                phone: '+998900000003',
                title: 'Home',
                full_address: 'Home st 2',
              },
            ],
          },
        ],
      },
    });

    adminService.searchAddresses.mockResolvedValue({
      data: {
        addresses: [
          {
            address_id: 9,
            title: 'Home',
            full_address: 'Home st 2',
            address_group_id: null,
            owner: { id: 33, first_name: 'Carol', last_name: 'Neighbor', phone: '+998900000003' },
          },
          {
            address_id: 2,
            title: 'Home',
            full_address: 'Home st 2',
            address_group_id: null,
            owner: { id: 11, first_name: 'Alice', last_name: 'Tester', phone: '+998900000001' },
          },
        ],
      },
    });
  });

  it('renders nothing without a user', () => {
    const { container } = render(<PlaceGroupPanel user={null} />, { wrapper: createWrapper() });
    expect(container.textContent).toBe('');
  });

  it('renders the group detail for a grouped address with cross-customer members', async () => {
    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await waitFor(() => expect(adminService.getPlaceGroup).toHaveBeenCalledWith(7));

    expect(await screen.findByText(/Acme office/)).toBeInTheDocument();
    // A place group spans customers: the coworker who does not own this
    // account's addresses must still be listed.
    expect(screen.getByText(/Bob Coworker/)).toBeInTheDocument();
    expect(screen.getByText('Bottles at this place')).toBeInTheDocument();
    // Assert the VALUE, not just the label. Checking the label alone is exactly
    // what let `place_union_balance` → `place_balance` land invisibly: the
    // statistic silently rendered its `?? 0` fallback and the test stayed green.
    expect(screen.getByText('5')).toBeInTheDocument();
    // ...and no per-member number beside it: the place's pool is indivisible,
    // so a "bottles: <n>" clause under a member could only ever be a fiction.
    expect(screen.queryByText(/bottles:/)).not.toBeInTheDocument();
    expect(screen.getByText('Place COD debt')).toBeInTheDocument();
    expect(screen.getByText('35,000')).toBeInTheDocument();
  });

  it('renders the place-group audit trail without the internal [group N] reason prefix', async () => {
    adminService.getPlaceGroup.mockResolvedValue({
      data: placeGroupDetail({
        place_group_id: 7,
        label: 'Acme office',
        place_balance: 0,
        members: [],
        cod: { total_outstanding_amount: 0, active_cod_debt_count: 0, items: [] },
        events: [
          {
            id: 51,
            event_type: 'create_place_group',
            acting_admin_id: 3,
            member_user_ids: [11, 22],
            reason: '[group 7] coworkers at one office',
            created_at: '2026-07-20T09:30:00+00:00',
          },
        ],
      }),
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    expect(await screen.findByText(/coworkers at one office/)).toBeInTheDocument();
    // The event type is rendered through `t()`, so the admin reads a sentence
    // rather than the raw `create_place_group` identifier.
    expect(screen.getByText(/Place group created/)).toBeInTheDocument();
    expect(screen.queryByText(/create_place_group/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\[group 7\]/)).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // P1: the un-anchored clusterer must not be billed on every drawer open
  // -------------------------------------------------------------------------
  // `get_place_group_suggestions` clusters the FULL ungrouped estate on every
  // call, uncached. That is not a bug to optimise away here: the PLACE channel
  // deliberately refuses to bbox-narrow the pool, because connected components
  // are transitively unbounded and a box would truncate a chain, making this
  // path disagree with `dismiss_place_suggestion` about a point's membership
  // and silently voiding the admin's dismissal (plan E19, pinned by
  // tests/unit/test_place_group_suggestions.py). The one-pool-one-clusterer
  // property must survive, so the fix is to stop paying for the pass unasked:
  // Users.js mounts this panel for EVERY customer drawer it opens.
  it('does not run the estate-wide co-location scan until it is asked for', async () => {
    const user = userEvent.setup();

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    // The rest of the panel still loads — only the scan is deferred.
    await waitFor(() => expect(adminService.getUserAddresses).toHaveBeenCalledWith(11));
    await screen.findByText(/Acme office/);
    expect(adminService.getPlaceGroupSuggestions).not.toHaveBeenCalled();

    // No suggestion row can be actioned before the scan has been requested —
    // an admin cannot group or dismiss a pair the panel has not computed.
    expect(screen.queryByRole('button', { name: /Group as same place/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Not the same place/ })).not.toBeInTheDocument();

    await revealSuggestions(user);

    await waitFor(() => expect(adminService.getPlaceGroupSuggestions).toHaveBeenCalledWith(11));
    expect(adminService.getPlaceGroupSuggestions).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole('button', { name: /Group as same place/ })).toBeInTheDocument();
  });

  // The drawer REUSES this component instance when the admin switches
  // customer, so consent must be anchored to the user it was given for. A
  // plain boolean would carry over and re-fire the full-estate scan on the
  // next customer without a click — reintroducing the exact cost removed above.
  it('does not carry a scan request over to the next customer', async () => {
    const user = userEvent.setup();

    const { rerender } = render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });
    await revealSuggestions(user);
    await waitFor(() => expect(adminService.getPlaceGroupSuggestions).toHaveBeenCalledWith(11));

    rerender(<PlaceGroupPanel user={{ ...USER, id: 12 }} />);

    expect(
      await screen.findByRole('button', { name: /Find possible same-place matches/ })
    ).toBeInTheDocument();
    expect(adminService.getPlaceGroupSuggestions).not.toHaveBeenCalledWith(12);
    expect(adminService.getPlaceGroupSuggestions).toHaveBeenCalledTimes(1);
  });

  it('creates a group from a suggestion with a required reason', async () => {
    const user = userEvent.setup();
    adminService.createPlaceGroup.mockResolvedValue({
      data: { place_group_id: 8, address_ids: [2, 9] },
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);

    // Reason is mandatory — the audit trail is worthless without it.
    expect(screen.getByRole('button', { name: /^OK$/ })).toBeDisabled();

    await openModalAndConfirm(user, 'coworkers at one office');

    // `toEqual` on the recorded call, not `toHaveBeenCalledWith`: the latter
    // does not short-circuit on arity, so a stray 5th argument would slip by.
    // The 4th is the merge review — `{}` when the admin never opened it, which
    // is what keeps a plain join byte-for-byte the join it always was.
    await waitFor(() =>
      expect(adminService.createPlaceGroup.mock.calls[0]).toEqual([
        [2, 9],
        null,
        'coworkers at one office',
        {},
      ])
    );
  });

  it('dismisses a suggestion with a reason and never calls the person-dismiss API', async () => {
    const user = userEvent.setup();
    adminService.dismissPlaceGroupSuggestion.mockResolvedValue({ data: {} });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Not the same place/);
    await openModalAndConfirm(user, 'different buildings');

    await waitFor(() =>
      expect(adminService.dismissPlaceGroupSuggestion).toHaveBeenCalledWith(
        2,
        9,
        'different buildings'
      )
    );
    // "Not the same place" must never imply "not the same person" (spec 10).
    expect(adminService.dismissCustomerLink).not.toHaveBeenCalled();
  });

  // Spec 9: "create a group from any customers' addresses (search-based
  // picker across users), add/remove members" — the suggestion engine only
  // surfaces co-located pairs, so without these two flows an admin can never
  // group a >50 m-apart coworker or add a new hire to an existing office.
  it('adds a manually searched address to an existing group', async () => {
    const user = userEvent.setup();
    adminService.addPlaceGroupAddresses.mockResolvedValue({
      data: { place_group_id: 7, address_ids: [1, 3, 9] },
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await user.click(await screen.findByRole('button', { name: /Add address/ }));

    const picker = await screen.findByRole('combobox');
    await user.type(picker, 'Carol');
    await waitFor(() => expect(adminService.searchAddresses).toHaveBeenCalledWith('Carol', true));

    await pickOption(user, /Carol Neighbor/);
    await openModalAndConfirm(user, 'new hire');

    await waitFor(() =>
      expect(adminService.addPlaceGroupAddresses.mock.calls[0]).toEqual([7, [9], 'new hire', {}])
    );
  });

  it('creates a group from two manually picked addresses', async () => {
    const user = userEvent.setup();
    adminService.createPlaceGroup.mockResolvedValue({
      data: { place_group_id: 9, address_ids: [2, 9] },
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await user.click(await screen.findByRole('button', { name: /New place group/ }));

    const picker = await screen.findByRole('combobox');
    await user.type(picker, 'Home');
    await waitFor(() => expect(adminService.searchAddresses).toHaveBeenCalledWith('Home', true));

    await pickOption(user, /Alice Tester/);
    await pickOption(user, /Carol Neighbor/);

    await user.type(screen.getByPlaceholderText(/Label/), 'Acme office');
    await openModalAndConfirm(user, 'reason');

    await waitFor(() =>
      expect(adminService.createPlaceGroup.mock.calls[0]).toEqual([
        [2, 9],
        'Acme office',
        'reason',
        {},
      ])
    );
  });

  it('removes a member address from its place group with a reason', async () => {
    const user = userEvent.setup();
    // The removal response carries the group id and how many bottles left WITH
    // the address (spec 7.1) — and nothing else. The retired ungroup-netting
    // payload is gone (spec 8), and `bottles_leaving` is a JSON number, not the
    // string a bare Decimal would serialise to.
    adminService.removePlaceGroupAddress.mockResolvedValue({
      data: { place_group_id: 7, bottles_leaving: 0 },
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    const bobRow = (await screen.findByText(/Bob Coworker/)).closest('li');
    await user.click(within(bobRow).getByRole('button', { name: /Remove/ }));
    await openModalAndConfirm(user, 'moved to another office');

    await waitFor(() =>
      expect(adminService.removePlaceGroupAddress.mock.calls[0]).toEqual([
        7,
        3,
        'moved to another office',
        // Bob's own attributed entries at this place, pre-filled by the backend.
        3,
      ])
    );
  });

  // Spec 7.1: some bottles may leave WITH the address. The backend has emitted
  // `suggested_bottles_leaving` per member since Task 2 and the panel sent three
  // arguments, so every removal silently defaulted to "everything stays with the
  // place" — data loss by default, on the one screen that can prevent it.
  it('pre-fills bottles leaving from the backend suggestion', async () => {
    const user = userEvent.setup();

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    const bobRow = (await screen.findByText(/Bob Coworker/)).closest('li');
    await user.click(within(bobRow).getByRole('button', { name: /Remove/ }));

    expect(await screen.findByDisplayValue('3')).toBeInTheDocument();
    // ...and the member's OWN suggestion, not the first member's or the place's.
    expect(screen.queryByDisplayValue('2')).not.toBeInTheDocument();
  });

  it('sends the bottles-leaving split the admin confirmed', async () => {
    const user = userEvent.setup();
    adminService.removePlaceGroupAddress.mockResolvedValue({
      data: { place_group_id: 7, bottles_leaving: 2, dissolved: false },
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    const bobRow = (await screen.findByText(/Bob Coworker/)).closest('li');
    await user.click(within(bobRow).getByRole('button', { name: /Remove/ }));

    await setNumber(user, await screen.findByRole('spinbutton'), '2');
    await openModalAndConfirm(user, 'left the office');

    await waitFor(() =>
      expect(adminService.removePlaceGroupAddress.mock.calls[0]).toEqual([
        7,
        3,
        'left the office',
        2,
      ])
    );
  });

  it('defaults the split to zero when the place holds nothing', async () => {
    const user = userEvent.setup();
    adminService.removePlaceGroupAddress.mockResolvedValue({
      data: { place_group_id: 7, bottles_leaving: 0, dissolved: false },
    });
    adminService.getPlaceGroup.mockResolvedValue({
      data: placeGroupDetail({
        place_group_id: 7,
        label: 'Acme office',
        // An over-returned place sits BELOW zero, so the cap is max(0, place):
        // any non-zero pre-fill here is a guaranteed PLACE_SPLIT_INVALID.
        place_balance: 0,
        members: [
          {
            address_id: 3,
            address_title: 'Office',
            full_address: 'Office st 1',
            suggested_bottles_leaving: 3,
            owner: { id: 22, first_name: 'Bob', last_name: 'Coworker', phone: '+998900000002' },
          },
        ],
        cod: { total_outstanding_amount: 0, active_cod_debt_count: 0, items: [] },
        events: [],
      }),
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    const bobRow = (await screen.findByText(/Bob Coworker/)).closest('li');
    await user.click(within(bobRow).getByRole('button', { name: /Remove/ }));

    expect(await screen.findByDisplayValue('0')).toBeInTheDocument();
    await openModalAndConfirm(user, 'moved');

    await waitFor(() =>
      expect(adminService.removePlaceGroupAddress.mock.calls[0]).toEqual([7, 3, 'moved', 0])
    );
  });

  // -------------------------------------------------------------------------
  // Merge review (spec 7.4)
  // -------------------------------------------------------------------------

  it('loads the merge preview before creating a group and forwards the reviewed set', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroupMergePreview.mockResolvedValue({ data: CLEAN_PREVIEW });
    adminService.createPlaceGroup.mockResolvedValue({ data: { place_group_id: 8 } });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);

    const merge = await openMergeReview(user);
    await waitFor(() =>
      expect(adminService.getPlaceGroupMergePreview).toHaveBeenCalledWith([2, 9], {
        groupId: undefined,
      })
    );

    // The merged history, one line per entry, with the MERGED running total.
    expect(within(merge).getByText(/Alice Tester/)).toBeInTheDocument();
    expect(within(merge).getByText(/\+4/)).toBeInTheDocument();

    const row = within(merge).getByText(/admin_adjustment/).closest('li');
    await user.click(within(row).getByRole('checkbox'));
    // The figures the admin decides against are the backend's, re-derived with
    // the exclusion applied — not arithmetic this panel invents.
    await waitFor(() =>
      expect(adminService.getPlaceGroupMergePreview).toHaveBeenLastCalledWith([2, 9], {
        groupId: undefined,
        exclude: [41],
      })
    );

    await setNumber(user, within(merge).getByRole('spinbutton'), '5');
    await user.click(within(merge).getByRole('button', { name: /^OK$/ }));

    await openModalAndConfirm(user, 'counted them');

    await waitFor(() =>
      expect(adminService.createPlaceGroup.mock.calls[0]).toEqual([
        [2, 9],
        null,
        'counted them',
        { excludedLedgerEntryIds: [41], resultingBalance: 5, previewEntryIds: [41, 42] },
      ])
    );
  });

  it('reviews an add against the group the addresses are joining', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroupMergePreview.mockResolvedValue({ data: CLEAN_PREVIEW });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await user.click(await screen.findByRole('button', { name: /Add address/ }));
    const picker = await screen.findByRole('combobox');
    await user.type(picker, 'Carol');
    await waitFor(() => expect(adminService.searchAddresses).toHaveBeenCalledWith('Carol', true));
    await pickOption(user, /Carol Neighbor/);

    await openMergeReview(user);

    // Without `groupId` the preview would show the joiner's history alone and
    // hide every bottle the place already holds.
    await waitFor(() =>
      expect(adminService.getPlaceGroupMergePreview).toHaveBeenCalledWith([9], { groupId: 7 })
    );
  });

  it('claims no balance decision the admin did not make', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroupMergePreview.mockResolvedValue({ data: CLEAN_PREVIEW });
    adminService.createPlaceGroup.mockResolvedValue({ data: { place_group_id: 8 } });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);
    const merge = await openMergeReview(user);
    await user.click(within(merge).getByRole('button', { name: /^OK$/ }));
    await openModalAndConfirm(user, 'looked fine');

    await waitFor(() => expect(adminService.createPlaceGroup).toHaveBeenCalled());
    const payload = adminService.createPlaceGroup.mock.calls[0][3];
    // Re-stating the previewed figure is arithmetically harmless (delta 0) but
    // writes a merge_backfill row and an audit trail claiming a decision that
    // was never made, so an untouched override must not be sent at all.
    //
    // ...and NEITHER may `previewEntryIds`. `_validate_merge_review` returns
    // early only when there is no review AND `preview_entry_ids is None`
    // (customer_link_service.py:832), so sending the ids alone arms the
    // staleness comparison while `_apply_merge_review` still writes nothing at
    // all (:1019). The entry set is then an input to no outcome whatsoever —
    // pure friction that can hard-REJECT a plain join, and re-clicking OK fails
    // forever because the ids never change.
    expect(Object.keys(payload).sort()).toEqual(['excludedLedgerEntryIds']);
    expect(payload.previewEntryIds).toBeUndefined();
  });

  it('lets a decision-free review through even after the ledger moves', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroupMergePreview.mockResolvedValue({ data: CLEAN_PREVIEW });
    adminService.addPlaceGroupAddresses.mockResolvedValue({ data: { place_group_id: 7 } });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await user.click(await screen.findByRole('button', { name: /Add address/ }));
    const picker = await screen.findByRole('combobox');
    await user.type(picker, 'Carol');
    await waitFor(() => expect(adminService.searchAddresses).toHaveBeenCalledWith('Carol', true));
    await pickOption(user, /Carol Neighbor/);

    const merge = await openMergeReview(user);
    await user.click(within(merge).getByRole('button', { name: /^OK$/ }));
    await openModalAndConfirm(user, 'new hire');

    // Looking is not deciding. Under the old three-argument service this join
    // could not be rejected for staleness; opening the review must not change
    // that, or an admin who ticks nothing is worse off for having looked.
    await waitFor(() =>
      expect(adminService.addPlaceGroupAddresses.mock.calls[0]).toEqual([
        7,
        [9],
        'new hire',
        { excludedLedgerEntryIds: [] },
      ])
    );
  });

  it('discards a confirmed review when the admin cancels a second look', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroupMergePreview.mockResolvedValue({ data: CLEAN_PREVIEW });
    adminService.createPlaceGroup.mockResolvedValue({ data: { place_group_id: 8 } });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);

    const merge = await openMergeReview(user);
    const row = within(merge).getByText(/admin_adjustment/).closest('li');
    await user.click(within(row).getByRole('checkbox'));
    await waitFor(() => expect(adminService.getPlaceGroupMergePreview).toHaveBeenCalledTimes(2));
    await user.click(within(merge).getByRole('button', { name: /^OK$/ }));

    // Second look, then Cancel. Every admin reads Cancel as "discard", so the
    // earlier exclusion — and its previewEntryIds — must not survive it.
    const reopened = await openMergeReview(user);
    await user.click(within(reopened).getByRole('button', { name: /^Cancel$/ }));

    await openModalAndConfirm(user, 'changed my mind');

    await waitFor(() =>
      expect(adminService.createPlaceGroup.mock.calls[0]).toEqual([
        [2, 9],
        null,
        'changed my mind',
        {},
      ])
    );
  });

  it('keeps the exclusion checkboxes describing the figures on screen', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroupMergePreview.mockResolvedValueOnce({ data: CLEAN_PREVIEW });
    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);
    const merge = await openMergeReview(user);

    // The re-derivation fails, so the four figures still describe the
    // PRE-exclusion set. A checkbox left ticked over them would state an
    // exclusion that nothing on screen has accounted for.
    adminService.getPlaceGroupMergePreview.mockRejectedValue(new Error('Network error'));
    const row = within(merge).getByText(/admin_adjustment/).closest('li');
    await user.click(within(row).getByRole('checkbox'));

    await waitFor(() => expect(message.error).toHaveBeenCalledWith('Network error'));
    await waitFor(() =>
      expect(within(row).getByRole('checkbox')).not.toBeChecked()
    );
  });

  it('reports an empty preview body instead of quietly doing nothing', async () => {
    const user = userEvent.setup();
    // A 200 whose envelope carries no `data`. The button would otherwise just
    // stop spinning and the admin would be left clicking it.
    adminService.getPlaceGroupMergePreview.mockResolvedValue({});

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);
    await user.click(screen.getByRole('button', { name: /Review bottle history/ }));

    await waitFor(() =>
      expect(message.error).toHaveBeenCalledWith('Could not load the merged history')
    );
    expect(screen.queryByText(/Review the merged bottle history/)).not.toBeInTheDocument();
  });

  it('states what a drifted place will actually hold, not just its ledger sum', async () => {
    const user = userEvent.setup();
    // Dev address 24's shape: stored 20.00 with ZERO ledger rows. The merge
    // aligns the ledger to the stored figure, so the place ends up holding 20 —
    // showing only the ledger-derived `resulting_balance` would tell the admin 0.
    adminService.getPlaceGroupMergePreview.mockResolvedValue({
      data: mergePreview({
        entries: [],
        entry_ids: [],
        computed_balance: 0,
        stored_balance: 20,
        drift: 20,
        excluded_total: 0,
        resulting_balance: 0,
        projected_place_balance: 20,
      }),
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);
    const merge = await openMergeReview(user);

    expect(within(merge).getByText('Unexplained drift')).toBeInTheDocument();
    expect(within(merge).getByText('Place will hold')).toBeInTheDocument();
    expect(within(merge).getByText('No bottle history to merge')).toBeInTheDocument();
    // The override starts at the number the place will really hold, so an admin
    // who accepts it is accepting what is actually about to happen.
    expect(within(merge).getByRole('spinbutton')).toHaveValue('20');
  });

  it('shows no drift on a clean place', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroupMergePreview.mockResolvedValue({ data: CLEAN_PREVIEW });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);
    const merge = await openMergeReview(user);

    expect(within(merge).getByText('Combined balance')).toBeInTheDocument();
    expect(within(merge).queryByText('Unexplained drift')).not.toBeInTheDocument();
    expect(within(merge).getByRole('spinbutton')).toHaveValue('7');
  });

  it('reports a failed preview instead of opening an empty review', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroupMergePreview.mockRejectedValue(new Error('Network error'));

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);
    await user.click(screen.getByRole('button', { name: /Review bottle history/ }));

    await waitFor(() => expect(message.error).toHaveBeenCalledWith('Network error'));
    expect(screen.queryByText(/Review the merged bottle history/)).not.toBeInTheDocument();
  });

  it('surfaces a stale preview by its error code, not the generic message', async () => {
    const user = userEvent.setup();
    adminService.createPlaceGroup.mockRejectedValue({
      response: {
        data: {
          success: false,
          message: 'Validation failed',
          data: { error_code: 'MERGE_PREVIEW_STALE' },
        },
      },
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);
    await openModalAndConfirm(user, 'counted them');

    await waitFor(() =>
      expect(message.error).toHaveBeenCalledWith(
        expect.stringContaining('changed while you were reviewing')
      )
    );
  });

  it('explains a rejected split instead of "Validation failed"', async () => {
    const user = userEvent.setup();
    adminService.removePlaceGroupAddress.mockRejectedValue({
      response: {
        data: {
          success: false,
          message: 'Validation failed',
          data: { error_code: 'PLACE_SPLIT_INVALID' },
        },
      },
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    const bobRow = (await screen.findByText(/Bob Coworker/)).closest('li');
    await user.click(within(bobRow).getByRole('button', { name: /Remove/ }));
    await openModalAndConfirm(user, 'moved');

    await waitFor(() =>
      expect(message.error).toHaveBeenCalledWith(
        expect.stringContaining('between 0 and the place total')
      )
    );
  });

  it('surfaces a specific reason for a rejected action instead of "Validation failed"', async () => {
    const user = userEvent.setup();
    adminService.createPlaceGroup.mockRejectedValue({
      response: {
        data: {
          success: false,
          message: 'Validation failed',
          errors: ['Grocery-store accounts cannot join place groups'],
          data: { error_code: 'PLACE_GROUP_GROCERY_MEMBER' },
        },
      },
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Group as same place/);
    await openModalAndConfirm(user, 'looks like one office');

    await waitFor(() => expect(message.error).toHaveBeenCalled());
    const shown = message.error.mock.calls[0][0];
    expect(shown).not.toBe('Validation failed');
    expect(shown).toMatch(/grocery/i);
  });

  it('reports a failed address search instead of silently showing no options', async () => {
    const user = userEvent.setup();
    adminService.searchAddresses.mockRejectedValue(new Error('Network error'));

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await user.click(await screen.findByRole('button', { name: /New place group/ }));
    await user.type(await screen.findByRole('combobox'), 'Carol');

    await waitFor(() => expect(message.error).toHaveBeenCalledWith('Network error'));
  });

  it('falls back to the backend errors[] prose when the error code is unknown', async () => {
    const user = userEvent.setup();
    adminService.dismissPlaceGroupSuggestion.mockRejectedValue({
      response: {
        data: {
          success: false,
          message: 'Validation failed',
          errors: ['Address not found'],
        },
      },
    });

    render(<PlaceGroupPanel user={USER} />, { wrapper: createWrapper() });

    await clickSuggestionAction(user, /Not the same place/);
    await openModalAndConfirm(user, 'different buildings');

    await waitFor(() => expect(message.error).toHaveBeenCalledWith('Address not found'));
  });
});
