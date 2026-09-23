import dayjs from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';

import { changedFields } from '../../../components/sales/OutletEditModal';

dayjs.extend(customParseFormat);

const OUTLET = {
  name: 'Bahor market', channel: null, preferred_visit_window: null, legal_form: null, tax_id: '305123456',
  competitor_note: null, status_warning: null, notes: 'Corner shop', class: 'B', cadence_days_override: null,
  payment_terms: 'cash', preferred_language: 'uz', delivery_window_start: '09:30', delivery_window_end: '18:45',
};
// Exactly what the form holds when the admin opens it and touches nothing.
const UNTOUCHED = {
  ...OUTLET,
  delivery_window_start: dayjs('09:30', 'HH:mm'),
  delivery_window_end: dayjs('18:45', 'HH:mm'),
};

it('sends nothing when the admin saves an untouched form', () => {
  expect(changedFields(OUTLET, UNTOUCHED)).toEqual({});
});

it('sends an emptied or whitespace-only text field as null', () => {
  expect(changedFields(OUTLET, { ...UNTOUCHED, notes: '', tax_id: '   ' })).toEqual({ notes: null, tax_id: null });
});

it('sends a cleared class as null and a new one as itself', () => {
  expect(changedFields(OUTLET, { ...UNTOUCHED, class: undefined })).toEqual({ class: null });
  expect(changedFields(OUTLET, { ...UNTOUCHED, class: 'A' })).toEqual({ class: 'A' });
});

it('sends the window as a pair when only one edge changed', () => {
  expect(changedFields(OUTLET, { ...UNTOUCHED, delivery_window_end: dayjs('19:00', 'HH:mm') }))
    .toEqual({ delivery_window_start: '09:30', delivery_window_end: '19:00' });
});
