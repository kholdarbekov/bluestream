/**
 * The antd Tag colour of an order status: one map for every page that draws an order's status
 * (the Orders page, and the "Earlier today" tags of the sales order-approval queue).
 */
export const getOrderStatusColor = (status) => {
  switch (status) {
    case 'pending':
      return 'orange';
    case 'confirmed':
      return 'blue';
    case 'preparing':
      return 'cyan';
    case 'out_for_delivery':
      return 'purple';
    case 'delivered':
      return 'green';
    case 'cancelled':
      return 'red';
    case 'returned':
      return 'volcano';
    default:
      return 'default';
  }
};
