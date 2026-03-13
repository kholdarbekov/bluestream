"""
Operator handler modules for Staff Bot
"""
from staff_bot.handlers.operator.create_user import CreateUserHandler, ENTER_PHONE, ENTER_FIRST_NAME, ENTER_LAST_NAME
from staff_bot.handlers.operator.create_user import SELECT_LANGUAGE as CREATE_USER_SELECT_LANGUAGE
from staff_bot.handlers.operator.create_user import CONFIRM_CREATE
from staff_bot.handlers.operator.search_user import SearchUserHandler, SEARCH_INPUT
from staff_bot.handlers.operator.create_order import CreateOrderHandler, SELECT_CLIENT, SELECT_ADDRESS, SELECT_PRODUCTS
from staff_bot.handlers.operator.create_order import SELECT_QUANTITY, SELECT_PAYMENT, ENTER_NOTES, CONFIRM_ORDER
from staff_bot.handlers.operator.manage_address import ManageAddressHandler, ENTER_LABEL, ENTER_ADDRESS
from staff_bot.handlers.operator.manage_address import ENTER_DISTRICT
from staff_bot.handlers.operator.manage_address import ENTER_NOTES as ADDR_ENTER_NOTES
from staff_bot.handlers.operator.manage_address import CONFIRM_ADDRESS
from staff_bot.handlers.operator.recent_orders import RecentOrdersHandler
from staff_bot.handlers.operator.orders_pool_view import OperatorOrdersPoolViewHandler

__all__ = [
    'CreateUserHandler',
    'SearchUserHandler',
    'CreateOrderHandler',
    'ManageAddressHandler',
    'RecentOrdersHandler',
    'OperatorOrdersPoolViewHandler',
]
