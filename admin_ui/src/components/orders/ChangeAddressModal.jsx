import { useState } from 'react';
import { Alert, Button, Input, Modal, Select, Space, Spin, message } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import adminService from '../../services/adminService';
import { apiErrorCode, extractApiErrorMessage } from '../../utils/apiError';
import { formatAddressLabel } from '../../utils/addressLabel';
import AsyncButton from '../common/AsyncButton';

const NOTE_MAX_LENGTH = 500;

// Every cached screen an address change makes wrong: the order, the dispatch map and its route
// line (a stop moved), the delivery list, the customer map pins (a confirmed order's place moved)
// and the dashboard counters.
export const CHANGE_ADDRESS_INVALIDATED_KEYS = [
  ['orders'],
  ['dispatchSnapshot'],
  ['dispatchSnapshotOverlay'],
  ['dispatchRouteGeometry'],
  ['deliveries'],
  ['customerMapPins'],
  ['dashboard'],
];

// Why an address is greyed or refused, as [translation key, English fallback]. Defined once: the
// options list (`disabled_reason`) and the write refusals (`error_code`) say the same five things.
const REASON_COPY = {
  CURRENT: ['ui.orders.change_address_reason_current', 'Current address'],
  NO_PIN: ['ui.orders.change_address_reason_no_pin', 'No map pin'],
  OUTSIDE_ZONE: ['ui.orders.change_address_reason_outside_zone', 'Outside the delivery area'],
  COD_CAPPED: ['ui.orders.change_address_reason_cod_capped', 'Cash limit reached at this place'],
  SELF_DECISION: ['ui.orders.change_address_reason_self_decision', 'Moves your own pay'],
};

/** `disabled_reason` (GET /address-options) -> [key, fallback]. Keyed by what the backend sends. */
export const ADDRESS_DISABLED_REASONS = {
  CURRENT: REASON_COPY.CURRENT,
  NO_PIN: REASON_COPY.NO_PIN,
  OUTSIDE_ZONE: REASON_COPY.OUTSIDE_ZONE,
  COD_PLACE_CAPPED: REASON_COPY.COD_CAPPED,
  SALES_PAY_SELF_DECISION: REASON_COPY.SELF_DECISION,
};

const LOCKED = ['ui.orders.change_address_error_locked', 'This order can no longer change its address.'];

/**
 * Refusal code -> [translation key, English fallback].
 *
 * PATCH /admin/orders/<id>/delivery-address and GET .../address-options answer these in
 * `data.error_code`. The modal names every code in the request's `handledErrorCodes`, so api.js
 * does not toast it and the admin reads the one inline message in their own language. A Map, as in
 * RescheduleOrderModal: the lookup key comes off the wire.
 */
export const CHANGE_ADDRESS_ERROR_MESSAGES = new Map([
  ['ORDER_ADDRESS_LOCKED', LOCKED],
  ['DELIVERY_ADDRESS_LOCKED', LOCKED],
  ['ORDER_AWAITING_STAFF_APPROVAL', ['ui.orders.awaiting_staff_approval', 'Awaiting manager approval']],
  ['INVALID_DELIVERY_ADDRESS', ['ui.orders.change_address_error_invalid', 'This address does not belong to the customer.']],
  ['ADDRESS_CURRENT', REASON_COPY.CURRENT],
  ['ADDRESS_NO_PIN', REASON_COPY.NO_PIN],
  ['ADDRESS_OUTSIDE_ZONE', REASON_COPY.OUTSIDE_ZONE],
  ['COD_DEBT_LIMIT_REACHED', REASON_COPY.COD_CAPPED],
  ['SALES_PAY_SELF_DECISION', REASON_COPY.SELF_DECISION],
]);

const HANDLED_ERROR_CODES = [...CHANGE_ADDRESS_ERROR_MESSAGES.keys()];

/** Translated copy for a known refusal code, else null. */
export const changeAddressErrorText = (code, t) => {
  const known = code ? CHANGE_ADDRESS_ERROR_MESSAGES.get(code) : null;
  return known ? t(known[0], known[1]) : null;
};

// A Map for the lookup: `disabled_reason` comes off the wire.
const DISABLED_REASONS = new Map(Object.entries(ADDRESS_DISABLED_REASONS));

const disabledReasonText = (reason, t) => {
  const known = reason ? DISABLED_REASONS.get(reason) : null;
  return known ? t(known[0], known[1]) : reason || '';
};

const optionLabel = (option, t) => {
  const label = formatAddressLabel(option, t);
  return option.selectable ? label : `${label} — ${disabledReasonText(option.disabled_reason, t)}`;
};

// A branch as the warning names it. A null branch is "no branch"; a null agent is a dash.
const branchName = (branch, t) => (branch ? branch.outlet_name : t('ui.orders.change_address_no_branch', 'no branch'));
const agentName = (branch) => branch?.agent_name || '—';

// A refusal, in the admin's language. A held order links to the queue that releases it (D4); the
// path is the backend's own (`details.path`), never built here.
const RefusalAlert = ({ error, style }) => {
  const { t } = useTranslation('orders');
  const code = apiErrorCode(error);
  const text = changeAddressErrorText(code, t)
    || extractApiErrorMessage(error, t('ui.common.error_occurred', 'An error occurred'));
  const path = error?.response?.data?.data?.details?.path;
  return (
    <Alert
      type="error"
      showIcon
      style={style}
      message={text}
      description={code === 'ORDER_AWAITING_STAFF_APPROVAL' && path ? (
        <Link to={path}>{t('ui.orders.open_order_approvals', 'Open order approvals')}</Link>
      ) : null}
    />
  );
};

const ChangeAddressForm = ({ orderId, data, onClose, onChanged }) => {
  const { t } = useTranslation('orders');
  const queryClient = useQueryClient();
  const [addressId, setAddressId] = useState(null);
  const [note, setNote] = useState('');

  const options = data.options || [];
  const selected = options.find((o) => o.id === addressId && o.selectable) || null;
  const nothingPickable = !options.some((o) => o.selectable);

  const mutation = useMutation({
    mutationFn: (payload) => adminService.changeOrderAddress(orderId, payload, { handledErrorCodes: HANDLED_ERROR_CODES }),
    onSuccess: (response) => {
      message.success(t('ui.orders.change_address_success', 'Delivery address changed'));
      CHANGE_ADDRESS_INVALIDATED_KEYS.forEach((queryKey) => queryClient.invalidateQueries({ queryKey }));
      onChanged?.(response?.data?.order);
      onClose();
    },
  });

  const save = () => {
    if (!selected) return;
    const trimmed = note.trim();
    mutation.mutate({ address_id: selected.id, ...(trimmed ? { note: trimmed } : {}) });
  };

  return (
    <div>
      {data.driver ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('ui.orders.change_address_driver_notice', 'Driver {{name}} will get the new address in the staff bot.', {
            name: data.driver.name,
          })}
        />
      ) : null}
      {nothingPickable ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('ui.orders.change_address_none_pickable', 'No other saved address can be picked. Add one on the Users page, then come back.')}
        />
      ) : null}
      {mutation.error ? <RefusalAlert error={mutation.error} style={{ marginBottom: 12 }} /> : null}
      {/* Read off the mutation, never copied into state (see RescheduleOrderModal). */}

      <div style={{ marginBottom: 4 }}>{t('ui.orders.change_address_select', 'New address')}</div>
      <Select
        style={{ width: '100%', marginBottom: 12 }}
        value={addressId}
        onChange={setAddressId}
        options={options.map((o) => {
          const label = optionLabel(o, t);
          return { value: o.id, label, title: label, disabled: !o.selectable };
        })}
      />

      {selected?.agent_differs ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t(
            'ui.orders.change_address_agent_warning',
            'This order will count for {{newBranch}} (agent {{newAgent}}) instead of {{oldBranch}} (agent {{oldAgent}}). Commission stays with the agent who placed the order.',
            {
              newBranch: branchName(selected.branch, t),
              newAgent: agentName(selected.branch),
              oldBranch: branchName(data.current_branch, t),
              oldAgent: agentName(data.current_branch),
            },
          )}
        />
      ) : null}

      <div style={{ marginBottom: 4 }}>{t('ui.orders.change_address_note', 'Note (optional)')}</div>
      <Input.TextArea rows={2} maxLength={NOTE_MAX_LENGTH} showCount value={note} onChange={(e) => setNote(e.target.value)} />

      <div style={{ marginTop: 16, textAlign: 'right' }}>
        <Space>
          <Button onClick={onClose}>{t('ui.common.cancel', 'Cancel')}</Button>
          <AsyncButton type="primary" onClick={save} disabled={!selected} loading={mutation.isPending}>
            {t('ui.common.save', 'Save')}
          </AsyncButton>
        </Space>
      </div>
    </div>
  );
};

const ChangeAddressBody = ({ orderId, onClose, onChanged }) => {
  // Read fresh on every open: the options, reasons and driver must describe the order as it is
  // now. One read per open, so a refetch cannot swap the list under a half-made choice.
  const query = useQuery({
    queryKey: ['order-address-options', orderId],
    queryFn: () => adminService.getOrderAddressOptions(orderId, { handledErrorCodes: HANDLED_ERROR_CODES }),
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });

  if (!query.isFetchedAfterMount) {
    return <div style={{ textAlign: 'center', padding: 24 }}><Spin /></div>;
  }
  if (query.isError || !query.data?.data) return <RefusalAlert error={query.error} />;
  return <ChangeAddressForm key={query.dataUpdatedAt} orderId={orderId} data={query.data.data} onClose={onClose} onChanged={onChanged} />;
};

/**
 * Move an order's delivery to another of the customer's saved addresses. Opened from the order
 * detail footer, gated on the order's published `can_change_address`. The backend decides every
 * option (selectable, reason, branch, agent_differs) and the modal computes nothing else.
 *
 * The body renders only while `open` and unmounts on close, so each open re-reads the options.
 */
const ChangeAddressModal = ({ orderId, open, onClose, onChanged }) => {
  const { t } = useTranslation('orders');
  return (
    <Modal
      title={t('ui.orders.change_address_title', 'Change delivery address')}
      open={open}
      onCancel={onClose}
      footer={null}
      width={560}
    >
      {open && orderId != null ? <ChangeAddressBody orderId={orderId} onClose={onClose} onChanged={onChanged} /> : null}
    </Modal>
  );
};

export default ChangeAddressModal;
