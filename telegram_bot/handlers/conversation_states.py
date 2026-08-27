"""PTB conversation-state constants shared across handler modules.

A leaf module with no imports of its own: several handler modules (address
flow, registration, phone verification, account linking) each need a subset
of these ints, and any one of them defining the full set would make the
others import from it, wiring in a dependency the constants have nothing to
do with. Kept here instead, with nothing importing it.
"""
# Conversation states.
# NOTE: renumbering these is safe. No `persistence` is configured on the
# Application (see telegram_bot/bot.py), so conversation state lives only in
# memory and already resets on every restart — no stored state can survive a
# deploy carrying a different numbering.
(SELECT_LANGUAGE, PHONE, ADDRESS_LOCATION, ADDRESS_TITLE,
 ADDRESS_REGION, ADDRESS_DISTRICT, ADDRESS_STREET, ADDRESS_BUILDING,
 ADDRESS_APARTMENT, ADDRESS_FLOOR,
 ADDRESS_DELIVERY_INSTRUCTIONS, ADDRESS_GEOCODE_CONFIRM,
 PHONE_VERIFY_PHONE, PHONE_VERIFY_NAME,
 LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP, REGISTER_OTP) = range(17)
