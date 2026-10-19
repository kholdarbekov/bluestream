/**
 * One line for a saved address: "Title: Full address (Default)".
 *
 * The Create Order picker, the order detail's Delivery address row, the Change address modal's
 * options and the Order Changes history all read an address through here, so they cannot word the
 * same place four ways. The history's address snapshot carries no `is_default`, so it reads
 * without the suffix. No address reads as ''; callers add their own dash.
 */
export const formatAddressLabel = (address, t) => {
  if (!address) return '';
  const { title, full_address: fullAddress, is_default: isDefault } = address;
  const suffix = isDefault ? ` (${t('ui.orders.address_default', 'Default')})` : '';
  return `${title ? `${title}: ` : ''}${fullAddress || ''}${suffix}`;
};

export default formatAddressLabel;
