"""Keyboards for the sales-agent flows (hub, lists, outlet card, onboarding, approvals)."""
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from staff_bot.i18n import i18n

# Stage decoration only. The WORDS come from `staff.sales.stage.<stage>`, whose
# family is registered in `staff_bot/i18n.py::_add_dynamic_family_keys` — the
# emoji is never the label, so a stage this map has not heard of still renders
# a translated name.
STAGE_EMOJI = {
    'prospect': '🆕', 'trial': '🧪', 'activation_requested': '⏳', 'active': '✅',
    'at_risk': '⚠️', 'dormant': '💤', 'lost': '❌',
}
PAGE_SIZE = 20
# Telegram truncates a long button label, and on the due list the label
# carries two different things: the shop's name and how late the call is.
LABEL_MAX = 60

# The rejection reasons an approver can pick, in the order they are drawn.
# ONE map rather than a tuple here and a lookup table in the handler: the
# handler validates the tapped reason against exactly the set this keyboard
# offered (the registered pattern is `\w+`, see bot.py), so a second copy of
# the list is a second place for the two to disagree.
#
# The KEY is both the callback suffix and the i18n tail
# (`staff.sales.approvals.reason.<key>`), so the button is localized. The VALUE
# is plain English on purpose: it is stored on the outlet and read back in the
# admin UI, where it must mean the same thing whatever language the operator
# who tapped it was using.
REJECT_REASONS = {
    'duplicate': 'Duplicate outlet',
    'incomplete': 'Incomplete data',
    'not_customer': 'Not a customer',
    'other': 'Other',
}
_REJECT_REASON_KEYS = {text: key for key, text in REJECT_REASONS.items()}

# ---- the visit vocabulary -------------------------------------------------
#
# ONE definition per list, in the module that draws the buttons, so the
# callback suffix, the i18n tail and the family loop in
# `staff_bot/i18n.py::_add_dynamic_family_keys` cannot disagree -- the
# REJECT_REASONS arrangement, one screen further down the funnel.
#
# These mirror `business_app/models/sales_visits.py` (VISIT_OUTCOMES,
# NO_ORDER_REASONS) and the backend's payment-method filter (D20). A value
# added there and not here renders a humanised key tail on an agent's phone
# while /health stays green.
#
# `scripts/seed_staff_translations.py` keeps a hand-written copy of these four
# tuples: it runs inside the business_app container, which has no `staff_bot/`
# tree, so it CANNOT import them. The copy is pinned by
# `tests/unit/test_sales_visit_bot_plumbing.py::
# test_the_seed_scripts_hand_written_tuples_match_the_keyboard_constants`.
VISIT_OUTCOMES = ('order_placed', 'no_order', 'closed', 'owner_absent', 'refused')
NO_ORDER_REASONS = ('sufficient_stock', 'cash_issue', 'price', 'competitor', 'other')
PAY_METHODS = ('cash', 'business_account')
NEXT_VISIT_CHOICES = ('3', '7', '14', '30', 'none')

# What a field photo is OF. Mirrors `ck_visit_photos_kind` on `visit_photos`
# (D17): the KEY is both the callback suffix and the i18n tail, the
# REJECT_REASONS arrangement one screen further down the funnel.
#
# `scripts/seed_staff_translations.py` keeps a hand-written copy -- it runs
# inside the business_app container, which has no `staff_bot/` tree to import
# from -- pinned by `tests/unit/test_sales_visit_bot_plumbing.py::
# test_the_seed_scripts_hand_written_tuples_match_the_keyboard_constants`.
PHOTO_KINDS = ('storefront', 'shelf', 'other')

# The three windows `GET /staff/sales/me/stats` answers, in the order the
# period switch draws them. A COPY of `business_app/utils/local_windows.py::
# STATS_PERIODS`, pinned by tests/unit/test_sales_visit_bot_plumbing.py: the
# backend decides what a "week" IS (local calendar, Monday..today) and
# answers with the dates it used, so this tuple decides only which three
# buttons exist and which values they may carry.
STATS_PERIODS = ('today', 'week', 'month')

# `order_placed` is stamped by `VisitService.close` whenever the visit carries
# an order, so it is never a button -- an agent must not be able to claim an
# order that does not exist. Derived rather than written out, so a sixth
# outcome becomes a button without a second edit.
CLOSE_OUTCOMES = tuple(outcome for outcome in VISIT_OUTCOMES if outcome != 'order_placed')

# The stages at which walking into a visit is worth the agent's time, used by
# the outlet card (Task 10) and by nothing else. A prospect is deliberately
# absent: nothing has been agreed, there is no customer account and no address
# row, so the visit loop's order step would have nothing to write against.
#
# The backend states the same rule twice more -- `OutletService`'s due-scope
# filter and `VisitService.place_order` -> SALES_OUTLET_NOT_ACTIVE -- and that
# duplication is deliberate, not drift: THIS tuple decides only what is
# OFFERED, and the backend is what refuses. If the two ever disagree the
# agent taps a button and reads a refusal, which is a bad screen but never a
# bad write.
VISITABLE_STAGES = ('active', 'at_risk', 'dormant', 'trial')

# The stages at which a try-out is the next move. `active` is deliberately
# absent -- a shop that already orders is not a prospect to convert -- and so
# is `lost`, which has been written off.
#
# The backend REFUSES no stage here: `OutletService.create_tryout_from_field`
# accepts a try-out on any outlet the agent may open and only TRANSITIONS
# `prospect|activation_requested` to `trial` (R9), leaving every other stage
# where it is. So this tuple has no server-side counterpart: it decides what is
# OFFERED and nothing more, and an offer widened here is a try-out the backend
# will accept -- unlike VISITABLE_STAGES, whose sibling rule is enforced.
TRYOUT_STAGES = ('prospect', 'activation_requested', 'trial')

# The quantity grids. Coarse on purpose: an agent standing in a shop taps
# once, and the long tail (7, 9, 11 ...) is rare enough to be worth re-tapping
# a nearby number rather than shipping a fifty-button wall.
STOCK_QTY_CHOICES = (0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50)
EMPTIES_CHOICES = (0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20)
# The order screen re-uses the shelf grid: the two questions ("how many are
# there" / "how many shall we bring") have the same useful answers, and one
# alias keeps Task 12's re-validation reading the tuple its button came from.
ORDER_QTY_CHOICES = STOCK_QTY_CHOICES
# The try-out grid, deliberately shorter than the order grid: a try-out is a
# SAMPLE, not a delivery, and the shop keeps it for a fortnight. `0` is on it
# because zeroing a line is how a product comes back off the list.
TRYOUT_QTY_CHOICES = (0, 1, 2, 3, 4, 5, 6)
GRID_COLUMNS = 3


def reject_reason_key(reason_text: str) -> Optional[str]:
    """The `REJECT_REASONS` key whose stored text is `reason_text`, or None.

    The reverse of what `approval_actions` sends, and the ONLY place that
    reversal happens: the backend stores `Outlet.rejected_reason` as the plain
    English string the operator's button carried, so the agent's rejection push
    needs the key back to render the label in THEIR language.

    A miss is normal, not an error — the admin UI can reject with free text —
    and the caller must then echo the raw string rather than substitute a label.
    """
    return _REJECT_REASON_KEYS.get((reason_text or '').strip())


def stage_label(stage: str, language: str) -> str:
    return f"{STAGE_EMOJI.get(stage, '•')} {i18n.get(f'staff.sales.stage.{stage}', language)}"


class SalesKeyboards:
    """Every inline keyboard the sales agent's own screens render."""

    @staticmethod
    def _back_to_hub(language: str) -> List[InlineKeyboardButton]:
        return [InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.sales.card.back_to_hub', language)}",
            callback_data="staff_sales_hub",
        )]

    @staticmethod
    def back_to_hub(language: str) -> InlineKeyboardMarkup:
        """A screen whose only exit is the hub -- the Nearby pin prompt.

        The row above, alone, as a keyboard. It matters that it is THIS row:
        `staff_sales_hub` is a fallback of every sales conversation, so the
        button both ends the flow and draws the destination exactly once.
        """
        return InlineKeyboardMarkup([SalesKeyboards._back_to_hub(language)])

    @staticmethod
    def _abandon_row(language: str) -> List[InlineKeyboardButton]:
        """The way out of the visit, on every screen inside one.

        The visit is server-side state: an agent who cannot end it from the
        screen they are on has to walk back out through the hub to do it, and
        `uq_visits_one_open_per_agent` refuses a second visit until they have.
        """
        return [InlineKeyboardButton(
            f"🚫 {i18n.get('staff.sales.visit.abandon', language)}",
            callback_data="staff_sales_v_abandon",
        )]

    @staticmethod
    def hub(language: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            # The due list leads because it IS the agent's work for the day.
            # `next_visit_due_at` is computed by `ReplenishmentService` (D7)
            # and the backend both filters and orders the `due` scope, so this
            # one button is the whole "where do I go now" answer — the agent
            # never scans "All" looking for it.
            [InlineKeyboardButton(
                f"⏰ {i18n.get('staff.sales.hub.due', language)}",
                callback_data="staff_sales_list_due",
            )],
            # Second, because it is what an agent reaches for once they are
            # already out: "what else is on this street". It is a CONVERSATION
            # entry (the pin has to be asked for), not a list scope -- which is
            # why `SalesHubHandler.show_list`'s allowlist deliberately stays
            # `due | prospects | all`: a `staff_sales_list_nearby` tap would ask
            # the backend to rank by a pin nobody shared.
            [InlineKeyboardButton(
                f"🧭 {i18n.get('staff.sales.hub.nearby', language)}",
                callback_data="staff_sales_nearby",
            )],
            [
                InlineKeyboardButton(
                    f"🆕 {i18n.get('staff.sales.hub.prospects', language)}",
                    callback_data="staff_sales_list_prospects",
                ),
                InlineKeyboardButton(
                    f"🏪 {i18n.get('staff.sales.hub.all', language)}",
                    callback_data="staff_sales_list_all",
                ),
            ],
            [InlineKeyboardButton(
                f"⬅️ {i18n.get('staff.back', language)}",
                callback_data="staff_back_to_main",
            )],
        ])

    @staticmethod
    def _outlet_button(outlet: Dict, suffix: str) -> InlineKeyboardButton:
        """One list row: the stage glyph, the shop's name, and the suffix.

        The SUFFIX is why the row is in this list at all — how late the call
        is on the due list, how far away the shop is on Nearby — so the NAME
        is what gives way when the label runs past `LABEL_MAX`. Slicing the
        composed label instead dropped the suffix behind any name long enough
        to fill the button, which is the one thing the row exists to say. One
        builder for both lists, so that rule cannot hold on one screen and
        not the other.
        """
        # Hoisted, not inlined as `outlet['id']`: the static scraper in
        # tests/unit/test_staff_bot_routing_regressions.py cannot read a
        # callback_data f-string containing an inner quote, and a literal
        # it cannot read is a literal it cannot check has a handler.
        outlet_id = outlet.get('id')
        head = f"{STAGE_EMOJI.get(outlet.get('stage'), '•')} "
        name = (outlet.get('name') or '')[:max(0, LABEL_MAX - len(head) - len(suffix))]
        return InlineKeyboardButton(
            f"{head}{name}{suffix}",
            callback_data=f"staff_sales_outlet_{outlet_id}",
        )

    @staticmethod
    def outlet_list(language: str, outlets: List[Dict], scope: str, page: int) -> InlineKeyboardMarkup:
        rows = []
        for outlet in outlets:
            # `overdue_days` is the backend's own arithmetic
            # (`serialize_outlet_agent_row`): None when the outlet has no due
            # date at all, 0 when the call is due today. Only a call that has
            # actually slipped earns the suffix — "+0d" would read as late.
            overdue_days = outlet.get('overdue_days')
            suffix = (
                f" · {i18n.get('staff.sales.list.overdue_suffix', language, days=overdue_days)}"
                if overdue_days else ''
            )
            rows.append([SalesKeyboards._outlet_button(outlet, suffix)])
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(
                f"◀️ {i18n.get('staff.sales.list.prev', language)}",
                callback_data=f"staff_sales_list_{scope}_{page - 1}",
            ))
        if len(outlets) >= PAGE_SIZE:
            nav.append(InlineKeyboardButton(
                f"{i18n.get('staff.sales.list.next', language)} ▶️",
                callback_data=f"staff_sales_list_{scope}_{page + 1}",
            ))
        if nav:
            rows.append(nav)
        rows.append(SalesKeyboards._back_to_hub(language))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def nearby_list(language: str, outlets: List[Dict]) -> InlineKeyboardMarkup:
        """The outlets around the pin, in the order the backend ranked them.

        `distance_m` is MEASURED server-side (`OutletService.nearby`, haversine
        from the agent's pin) and so is the ordering and the length of the
        list (`SALES_NEARBY_LIMIT`). This draws the rows it was handed, in the
        order it was handed them, and never sorts, filters or converts: an
        outlet the backend published no distance for keeps its row and loses
        its suffix, exactly as an outlet with no due date loses the overdue
        one. Rounding to whole metres is the only arithmetic here — a tenth of
        a metre is noise on a phone fix.

        No pagination: the backend answers the top N and there is no page 2 to
        ask for. 📍 New pin is the way to change the answer, and it is the
        conversation's entry point as well as a button on this screen, so it
        still works on a keyboard that outlived a bot restart.
        """
        rows = []
        for outlet in outlets:
            distance = outlet.get('distance_m')
            suffix = '' if distance is None else (
                f" · {i18n.get('staff.sales.nearby.distance', language, distance=int(round(float(distance))))}"
            )
            rows.append([SalesKeyboards._outlet_button(outlet, suffix)])
        rows.append([InlineKeyboardButton(
            f"📍 {i18n.get('staff.sales.nearby.again', language)}",
            callback_data="staff_sales_nb_again",
        )])
        rows.append(SalesKeyboards._back_to_hub(language))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _resume_button(language: str) -> InlineKeyboardButton:
        """⏯ Resume visit — the visit conversation's ENTRY literal.

        Drawn on the outlet card and on the "it may have landed" screen, and
        built ONCE: the callback is an entry point, so a rename in one of two
        copies leaves a live-looking button that starts nothing.
        """
        return InlineKeyboardButton(
            f"⏯ {i18n.get('staff.sales.card.resume_visit', language)}",
            callback_data="staff_sales_visit_resume",
        )

    @staticmethod
    def outlet_card(language: str, outlet: Dict) -> InlineKeyboardMarkup:
        rows = []
        outlet_id = outlet.get('id')
        # An agent may hold ONE open visit at a time
        # (`uq_visits_one_open_per_agent`), and the backend publishes which
        # visit is open at THIS outlet. Offering "Start" on top of it posts
        # into a 409 the agent can do nothing with, so the two buttons are
        # mutually exclusive and the server's field — never a cached
        # `user_data` flag — decides which one is drawn.
        open_visit_id = outlet.get('open_visit_id')
        if open_visit_id:
            rows.append([SalesKeyboards._resume_button(language)])
        elif outlet.get('stage') in VISITABLE_STAGES:
            rows.append([InlineKeyboardButton(
                f"▶️ {i18n.get('staff.sales.card.start_visit', language)}",
                callback_data=f"staff_sales_visit_start_{outlet_id}",
            )])
        if outlet.get('stage') in ('prospect', 'trial') and outlet.get('outlet_type') != 'individual':
            rows.append([InlineKeyboardButton(
                f"🚀 {i18n.get('staff.sales.card.request_activation', language)}",
                callback_data=f"staff_sales_activate_{outlet_id}",
            )])
        if outlet.get('stage') in TRYOUT_STAGES:
            rows.append([InlineKeyboardButton(
                f"🧪 {i18n.get('staff.sales.tryout.button', language)}",
                callback_data=f"staff_sales_tryout_{outlet_id}",
            )])
        if outlet.get('latitude') is not None:
            url = f"https://yandex.uz/maps/?rtext=~{outlet['latitude']},{outlet['longitude']}&rtt=auto"
            rows.append([InlineKeyboardButton(
                f"🗺 {i18n.get('staff.sales.card.navigate', language)}",
                url=url,
            )])
        rows.append(SalesKeyboards._back_to_hub(language))
        return InlineKeyboardMarkup(rows)

    # ---- field onboarding ("New outlet") -------------------------------------

    @staticmethod
    def outlet_type(language: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"🏪 {i18n.get('staff.sales.type.grocery_store', language)}",
                callback_data="staff_sales_no_type_grocery_store",
            )],
            [InlineKeyboardButton(
                f"🏢 {i18n.get('staff.sales.type.workplace', language)}",
                callback_data="staff_sales_no_type_workplace",
            )],
            [InlineKeyboardButton(
                f"👤 {i18n.get('staff.sales.type.individual', language)}",
                callback_data="staff_sales_no_type_individual",
            )],
            [InlineKeyboardButton(
                f"❌ {i18n.get('staff.cancel', language)}",
                callback_data="staff_back_to_main",
            )],
        ])

    @staticmethod
    def skip(language: str, step: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            f"⏭ {i18n.get('staff.sales.new.skip', language)}",
            callback_data=f"staff_sales_no_skip_{step}",
        )]])

    @staticmethod
    def outlet_class(language: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("A", callback_data="staff_sales_no_class_A"),
                InlineKeyboardButton("B", callback_data="staff_sales_no_class_B"),
                InlineKeyboardButton("C", callback_data="staff_sales_no_class_C"),
            ],
            [InlineKeyboardButton(
                f"⏭ {i18n.get('staff.sales.new.skip', language)}",
                callback_data="staff_sales_no_class_skip",
            )],
        ])

    @staticmethod
    def confirm_outlet(language: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ {i18n.get('staff.confirm', language)}",
                callback_data="staff_sales_no_confirm",
            ),
            InlineKeyboardButton(
                f"❌ {i18n.get('staff.cancel', language)}",
                callback_data="staff_back_to_main",
            ),
        ]])

    @staticmethod
    def duplicate_choice(language: str, candidates: List[Dict]) -> InlineKeyboardMarkup:
        """What to do about the near-duplicates the backend found.

        A `customer` candidate is a person we already know, so the outlet is
        LINKED to their account; an `outlet` candidate is a record we already
        have, so the only sane action is to open it rather than create a
        second one. The escape hatch is last, never first.
        """
        rows = []
        for candidate in candidates[:5]:
            name = (candidate.get('name') or '')[:40]
            # Both ids are hoisted out of the f-string on purpose. The static
            # literal guard (tests/unit/test_staff_bot_routing_regressions.py)
            # scrapes `callback_data=f"..."` with a regex that stops at the
            # first quote, so `f"..._{candidate['user_id']}"` is invisible to
            # it -- a button nothing checks has a registered handler for.
            user_id = candidate.get('user_id')
            outlet_id = candidate.get('outlet_id')
            if candidate.get('kind') == 'customer' and user_id:
                rows.append([InlineKeyboardButton(
                    f"🔗 {i18n.get('staff.sales.new.link_existing', language)}: {name}",
                    callback_data=f"staff_sales_no_link_{user_id}",
                )])
            elif outlet_id:
                rows.append([InlineKeyboardButton(
                    f"📍 {i18n.get('staff.sales.new.open_existing', language)}: {name}",
                    callback_data=f"staff_sales_outlet_{outlet_id}",
                )])
        rows.append([InlineKeyboardButton(
            f"➕ {i18n.get('staff.sales.new.create_anyway', language)}",
            callback_data="staff_sales_no_force",
        )])
        rows.append([InlineKeyboardButton(
            f"❌ {i18n.get('staff.cancel', language)}",
            callback_data="staff_back_to_main",
        )])
        return InlineKeyboardMarkup(rows)

    # ---- operator approvals ("Activation requests") ---------------------------

    @staticmethod
    def _back_to_main(language: str) -> List[InlineKeyboardButton]:
        return [InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_back_to_main",
        )]

    @staticmethod
    def approvals_list(language: str, outlets: List[Dict]) -> InlineKeyboardMarkup:
        rows = []
        for outlet in outlets[:PAGE_SIZE]:
            # The id is hoisted out of the f-string so the static literal guard
            # (tests/unit/test_staff_bot_routing_regressions.py) can read this
            # button. Its scraper is
            # `callback_data\s*=\s*f?(['"])([^'"]+)\1`, whose body class cannot
            # span the inner quote of `o['id']`, so that shape matches NOTHING
            # -- the literal is not captured at all, and a button the guard
            # never sees is a button nobody checks has a handler for.
            outlet_id = outlet.get('id')
            rows.append([InlineKeyboardButton(
                f"⏳ {outlet.get('name', '')}"[:60],
                callback_data=f"staff_sales_review_{outlet_id}",
            )])
        rows.append(SalesKeyboards._back_to_main(language))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def approval_result(language: str) -> InlineKeyboardMarkup:
        """Where an operator lands after approving or rejecting one request.

        Back to the QUEUE first, main menu second. They are working a list, so
        the next thing they want is the next request; leaving only "Back" (to
        the main menu) meant a round trip through Profile between every single
        decision.
        """
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"⬅️ {i18n.get('staff.sales.approvals.back', language)}",
                callback_data="staff_sales_approvals",
            )],
            SalesKeyboards._back_to_main(language),
        ])

    @staticmethod
    def approval_actions(language: str, outlet_id: int) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(
            f"✅ {i18n.get('staff.sales.approvals.approve', language)}",
            callback_data=f"staff_sales_approve_{outlet_id}",
        )]]
        # Two reasons per row, drawn from REJECT_REASONS so the buttons and the
        # handler's allowlist can never drift apart.
        reasons = [
            InlineKeyboardButton(
                f"❌ {i18n.get(f'staff.sales.approvals.reason.{reason}', language)}",
                callback_data=f"staff_sales_reject_{outlet_id}_{reason}",
            )
            for reason in REJECT_REASONS
        ]
        rows.extend(reasons[index:index + 2] for index in range(0, len(reasons), 2))
        rows.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.sales.approvals.back', language)}",
            callback_data="staff_sales_approvals",
        )])
        return InlineKeyboardMarkup(rows)

    # ---- the visit loop -------------------------------------------------------
    #
    # Every callback prefix below is written out as a LITERAL inside its own
    # f-string. The static guard
    # (`tests/unit/test_staff_bot_routing_regressions.py::_materialize_literal`)
    # rewrites every `{...}` to `1`, so a shared helper emitting the prefix as
    # an interpolated variable -- `staff_sales_v_<prefix>_<product_id>_<n>` --
    # would materialise as `staff_sales_v_1_1_1`: a literal no registered
    # pattern can match, and the guard would then demand a
    # `^staff_sales_v_\d+_\d+_\d+$` handler that must never exist. Ids are
    # hoisted into locals for the same reason the approvals list hoists them:
    # the scraper's body class cannot span an inner quote.

    @staticmethod
    def _grid_rows(buttons: List[InlineKeyboardButton]) -> List[List[InlineKeyboardButton]]:
        """Chunk a flat list of number buttons into `GRID_COLUMNS`-wide rows."""
        return [
            buttons[index:index + GRID_COLUMNS]
            for index in range(0, len(buttons), GRID_COLUMNS)
        ]

    @staticmethod
    def checkin(language: str) -> InlineKeyboardMarkup:
        """Check-in screen.

        The pin itself arrives on a REPLY keyboard (`request_location` exists
        only there), so this inline pair carries the two escapes: skip the
        pin, or abandon the visit. Check-in never blocks the visit -- a skip
        is recorded, not refused.
        """
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"⏭ {i18n.get('staff.sales.visit.checkin_skip', language)}",
                callback_data="staff_sales_v_skipcheckin",
            )],
            [InlineKeyboardButton(
                f"🚫 {i18n.get('staff.sales.visit.abandon', language)}",
                callback_data="staff_sales_v_abandon",
            )],
        ])

    @staticmethod
    def resume_or_abandon(language: str) -> InlineKeyboardMarkup:
        """What the bot offers after a 409 SALES_VISIT_ALREADY_OPEN."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"⏯ {i18n.get('staff.sales.visit.resume', language)}",
                callback_data="staff_sales_v_resume",
            )],
            [InlineKeyboardButton(
                f"🚫 {i18n.get('staff.sales.visit.abandon', language)}",
                callback_data="staff_sales_v_abandon",
            )],
        ])

    @staticmethod
    def visit_resume(language: str) -> InlineKeyboardMarkup:
        """Reopen the visit from OUTSIDE the conversation.

        `staff_sales_v_resume` is a state-scoped button on the 409 offer;
        this one carries the conversation's own ENTRY point, so it still works
        after the conversation has ended -- which is exactly the case plan
        ruling 30 covers: a `POST /order` that timed out may or may not have
        landed, and the only honest next move is "open the visit and look".
        """
        return InlineKeyboardMarkup([[SalesKeyboards._resume_button(language)]])

    @staticmethod
    def stock_overview(language: str, rows: List[Dict], done_enabled: bool,
                       *, can_retry: bool = False) -> InlineKeyboardMarkup:
        """One button per stock-check product, labelled with what is on the shelf.

        `rows` are the ruling-32 shape: a `serialize_stock_check` row
        (`product_id`, `product_name`, `on_hand_qty`, `empties_qty`,
        `is_sold_out`, `is_low`, `suggested_qty`, `accepted_qty`,
        `rate_per_day`, `rate_source`) merged with the product's
        `is_returnable_bottle` / `min_order_quantity`. Six keys are read here;
        the rest ride along so the overview, the quantity screen and the order
        screen all speak ONE dict.

        `on_hand_qty is None` means not counted yet and prints NOTHING -- a
        `0` would read as "checked, and the shelf is empty", which is a
        different fact and one the rate rule would act on. *Done* appears only
        once something has been counted.

        The last button is one of three, and never none of them -- a screen
        whose only move is Abandon is the dead end review #01/#49/#23 is about:

        * `can_retry` (the catalogue could not be FETCHED): *Try again*. No
          Continue, because posting an empty count here would record "nothing
          on this shelf" for a shelf nobody managed to look at.
        * something counted: *Done*.
        * an EMPTY catalogue that was fetched fine (every install until an
          admin ticks `products.in_sales_stock_check`): *Continue*, which posts
          `items: []` -- a submission the backend accepts, moving the visit to
          the order step where the close path lives.
        """
        keyboard = []
        for row in rows:
            product_id = row.get('product_id')
            on_hand = row.get('on_hand_qty')
            label = row.get('product_name') or ''
            if on_hand is not None:
                label = f"{label} · {on_hand}"
            if row.get('is_sold_out'):
                label = f"{label} 🚫"
            if row.get('is_low'):
                label = f"{label} ⚠️"
            keyboard.append([InlineKeyboardButton(
                label[:60],
                callback_data=f"staff_sales_v_stock_{product_id}",
            )])
        if can_retry:
            keyboard.append([InlineKeyboardButton(
                f"🔄 {i18n.get('staff.sales.visit.stock_retry', language)}",
                callback_data="staff_sales_v_stockretry",
            )])
        elif done_enabled:
            keyboard.append([InlineKeyboardButton(
                f"✅ {i18n.get('staff.sales.visit.stock_done', language)}",
                callback_data="staff_sales_v_stockdone",
            )])
        elif not rows:
            # Reuses `order_done` ("Continue"): the same word on the same kind
            # of button, and a second key would be a second thing to translate.
            keyboard.append([InlineKeyboardButton(
                f"➡️ {i18n.get('staff.sales.visit.order_done', language)}",
                callback_data="staff_sales_v_stockdone",
            )])
        keyboard.append([InlineKeyboardButton(
            f"🚫 {i18n.get('staff.sales.visit.abandon', language)}",
            callback_data="staff_sales_v_abandon",
        )])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def stock_quantity(
        language: str,
        product_id: int,
        on_hand: Optional[int],
        is_returnable: bool,
        empties: Optional[int],
        sold_out: bool,
        low: bool,
    ) -> InlineKeyboardMarkup:
        """One product's shelf count: on-hand grid, empties grid, flags, back.

        `on_hand`/`empties` are Optional and `None` marks NOTHING: an
        uncounted product must not open with `0` highlighted, or the agent
        reads a count they never made. The `•` marker is also the only
        feedback that a tap landed at all -- Telegram redraws an identical
        grid otherwise.

        The empties grid is drawn only for a returnable SKU, and the caller
        decides that from the backend's `is_returnable_bottle`
        (`Product.is_returnable_bottle` is the SSOT) rather than from the
        product's name -- a 10L SKU mis-flagged once already booked seven
        bottles for three.
        """
        rows = []
        quantity_buttons = [
            InlineKeyboardButton(
                f"• {step}" if on_hand is not None and step == on_hand else str(step),
                callback_data=f"staff_sales_v_qty_{product_id}_{step}",
            )
            for step in STOCK_QTY_CHOICES
        ]
        rows.extend(SalesKeyboards._grid_rows(quantity_buttons))
        if is_returnable:
            empties_buttons = [
                InlineKeyboardButton(
                    f"♻️ • {step}" if empties is not None and step == empties else f"♻️ {step}",
                    callback_data=f"staff_sales_v_empt_{product_id}_{step}",
                )
                for step in EMPTIES_CHOICES
            ]
            rows.extend(SalesKeyboards._grid_rows(empties_buttons))
        flags = [InlineKeyboardButton(
            f"{'☑' if sold_out else '☐'} {i18n.get('staff.sales.visit.sold_out', language)}",
            callback_data=f"staff_sales_v_soldout_{product_id}",
        )]
        # Low is drawn only once the shelf HAS a count. Sold out implies an
        # obvious quantity (zero, which `toggle_sold_out` writes); Low implies
        # none, and a flag on an uncounted product is silently dropped from the
        # POST by `VisitHandler._stock_items` while the overview still
        # decorates the button with ⚠️.
        if on_hand is not None:
            flags.append(InlineKeyboardButton(
                f"{'☑' if low else '☐'} {i18n.get('staff.sales.visit.low', language)}",
                callback_data=f"staff_sales_v_low_{product_id}",
            ))
        rows.append(flags)
        rows.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.sales.visit.stock_back', language)}",
            callback_data="staff_sales_v_stockback",
        )])
        rows.append(SalesKeyboards._abandon_row(language))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def order_options(language: str, has_suggestion: bool, has_last: bool) -> InlineKeyboardMarkup:
        """The four ways out of the order step, plus the way out of the visit.

        *Order suggested* and *Same as last order* are drawn only when the
        backend actually supplied a suggestion / a last order: a button that
        would post an empty basket is worse than no button. Abandon is drawn
        always, exactly as on the shelf overview -- the visit is server-side
        state, and every screen inside it has to be able to end it.
        """
        rows = []
        if has_suggestion:
            rows.append([InlineKeyboardButton(
                f"✅ {i18n.get('staff.sales.visit.order_suggested', language)}",
                callback_data="staff_sales_v_ordersugg",
            )])
        rows.append([InlineKeyboardButton(
            f"✏️ {i18n.get('staff.sales.visit.order_edit', language)}",
            callback_data="staff_sales_v_orderedit",
        )])
        if has_last:
            rows.append([InlineKeyboardButton(
                f"🔁 {i18n.get('staff.sales.visit.order_last', language)}",
                callback_data="staff_sales_v_orderlast",
            )])
        rows.append([InlineKeyboardButton(
            f"⏭ {i18n.get('staff.sales.visit.no_order', language)}",
            callback_data="staff_sales_v_noorder",
        )])
        rows.append([InlineKeyboardButton(
            f"🚫 {i18n.get('staff.sales.visit.abandon', language)}",
            callback_data="staff_sales_v_abandon",
        )])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def order_edit(language: str, lines: List[Dict]) -> InlineKeyboardMarkup:
        """The basket, one tappable line per product, then Continue.

        `lines` are `{product_id, product_name, quantity}` (ruling 32) --
        `product_name` is `serialize_stock_check`'s name, so the basket, the
        shelf and the confirm screen all read one key.
        """
        rows = []
        for line in lines:
            product_id = line.get('product_id')
            label = f"{line.get('product_name', '')} × {line.get('quantity', 0)}"
            rows.append([InlineKeyboardButton(
                label[:60],
                callback_data=f"staff_sales_v_oqty_{product_id}",
            )])
        rows.append([InlineKeyboardButton(
            f"✅ {i18n.get('staff.sales.visit.order_done', language)}",
            callback_data="staff_sales_v_orderdone",
        )])
        # Back to the order options, which is where the *No order* path lives:
        # a basket with no route out of it made that path unreachable once the
        # agent had opened one. Same literal and same handler as the confirm
        # screen's Back — one button, registered in both states.
        rows.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.sales.visit.back', language)}",
            callback_data="staff_sales_v_orderback",
        )])
        rows.append(SalesKeyboards._abandon_row(language))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def order_quantity(language: str, product_id: int, current: Optional[int]) -> InlineKeyboardMarkup:
        """The quantity grid for ONE basket line. `0` removes the line."""
        buttons = [
            InlineKeyboardButton(
                f"• {step}" if current is not None and step == current else str(step),
                callback_data=f"staff_sales_v_oqtyset_{product_id}_{step}",
            )
            for step in ORDER_QTY_CHOICES
        ]
        rows = SalesKeyboards._grid_rows(buttons)
        rows.append(SalesKeyboards._abandon_row(language))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def no_order_reasons(language: str) -> InlineKeyboardMarkup:
        """Why the visit produced no order, two per row.

        Drawn from `NO_ORDER_REASONS`, which mirrors the backend's
        `ck_visits_no_order_reason` CHECK -- a button offering a value the
        database refuses is a 400 the agent cannot act on.
        """
        buttons = [
            InlineKeyboardButton(
                i18n.get(f'staff.sales.visit.reason.{reason}', language),
                callback_data=f"staff_sales_v_noreason_{reason}",
            )
            for reason in NO_ORDER_REASONS
        ]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        rows.append(SalesKeyboards._abandon_row(language))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def payment_methods(language: str, methods: List[Dict]) -> InlineKeyboardMarkup:
        """The payment methods the BACKEND allowed for this outlet.

        `methods` is `GET /outlets/<id>/payment-methods` verbatim -- the
        `[{method, name, description}]` rows (ruling 32), not a list the
        caller flattened. Anything outside `PAY_METHODS` is dropped rather
        than drawn: the staff visit path offers cash and the business account
        only (D20), and a Click button here would send the agent down a link
        the staff API cannot create.

        The label is `staff.sales.visit.pay.<method>`, and the row's own `name`
        is deliberately NOT preferred: `StaffService.get_client_payment_methods`
        hard-codes "Cash on Delivery" / "Business Account" as plain English
        literals, so preferring it shipped English to every uz/ru agent while
        /health stayed green -- and made one flow say two different words for
        the same rail, because the confirm card renders the family key anyway.
        Safe because the loop has already filtered to `PAY_METHODS`, which is
        exactly the set the family is registered for.
        """
        rows = []
        for entry in methods:
            method = entry.get('method') if isinstance(entry, dict) else entry
            if method not in PAY_METHODS:
                continue
            name = i18n.get(f'staff.sales.visit.pay.{method}', language)
            rows.append([InlineKeyboardButton(
                f"💳 {name}"[:60],
                callback_data=f"staff_sales_v_pay_{method}",
            )])
        rows.append([InlineKeyboardButton(
            f"🚫 {i18n.get('staff.sales.visit.abandon', language)}",
            callback_data="staff_sales_v_abandon",
        )])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def delivery_day(language: str) -> InlineKeyboardMarkup:
        """Tomorrow (the default), today, or a typed date."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📅 {i18n.get('staff.sales.visit.day_tomorrow', language)}",
                callback_data="staff_sales_v_day_tomorrow",
            )],
            [InlineKeyboardButton(
                f"⚡ {i18n.get('staff.sales.visit.day_today', language)}",
                callback_data="staff_sales_v_day_today",
            )],
            [InlineKeyboardButton(
                f"🗓 {i18n.get('staff.sales.visit.day_pick', language)}",
                callback_data="staff_sales_v_day_pick",
            )],
            # Its OWN literal, not the confirm screen's: Back from here means
            # "the last question I was asked", which is the rail when there
            # was a rail to pick. `staff_sales_v_day_\w+` cannot match it —
            # there is no underscore after `day` — so the two never collide.
            [InlineKeyboardButton(
                f"⬅️ {i18n.get('staff.sales.visit.back', language)}",
                callback_data="staff_sales_v_dayback",
            )],
            SalesKeyboards._abandon_row(language),
        ])

    @staticmethod
    def visit_skip(language: str, step: str) -> InlineKeyboardMarkup:
        """Skip an optional free-text step (`notes`, `closenotes`).

        Reuses `staff.sales.new.skip`: the same word on the same kind of
        button, and a second key would be a second thing to translate.
        """
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"⏭ {i18n.get('staff.sales.new.skip', language)}",
                callback_data=f"staff_sales_v_skip_{step}",
            )],
            SalesKeyboards._abandon_row(language),
        ])

    @staticmethod
    def order_confirm(language: str) -> InlineKeyboardMarkup:
        """The last screen before `POST /visits/<id>/order`.

        *Back* returns to the order options; a failed POST keeps the agent
        HERE (the operator precedent) rather than dropping them into the
        close step with no order and no explanation.
        """
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"✅ {i18n.get('staff.sales.visit.confirm', language)}",
                    callback_data="staff_sales_v_orderconfirm",
                ),
                InlineKeyboardButton(
                    f"⬅️ {i18n.get('staff.sales.visit.back', language)}",
                    callback_data="staff_sales_v_orderback",
                ),
            ],
            SalesKeyboards._abandon_row(language),
        ])

    @staticmethod
    def close_outcome(language: str) -> InlineKeyboardMarkup:
        """How the visit ended, when the backend has not already decided."""
        rows = [
            [InlineKeyboardButton(
                i18n.get(f'staff.sales.visit.outcome.{outcome}', language),
                callback_data=f"staff_sales_v_outcome_{outcome}",
            )]
            for outcome in CLOSE_OUTCOMES
        ]
        rows.append(SalesKeyboards._abandon_row(language))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def next_visit(language: str) -> InlineKeyboardMarkup:
        """The agent's own next-visit date, three per row.

        It is only ONE of the three inputs to `next_visit_due_at` (D7); the
        backend takes the earliest of the agent's date, the predicted
        stock-out and the class cadence, and publishes the answer.
        """
        buttons = [
            InlineKeyboardButton(
                i18n.get(f'staff.sales.visit.next.{choice}', language),
                callback_data=f"staff_sales_v_next_{choice}",
            )
            for choice in NEXT_VISIT_CHOICES
        ]
        rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
        rows.append(SalesKeyboards._abandon_row(language))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def photo_kind(language: str) -> InlineKeyboardMarkup:
        """What the photo that just arrived is of.

        Deliberately NO abandon row: this is not a step of the visit. It
        answers a message, the step screen is untouched above it, and every
        handler behind these buttons returns None -- which PTB reads as
        "state unchanged".
        """
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                i18n.get(f'staff.sales.visit.photo_kind.{kind}', language),
                callback_data=f"staff_sales_v_photo_{kind}",
            )]
            for kind in PHOTO_KINDS
        ])

    # ---- try-out from the field -----------------------------------------------
    #
    # The same literal rule as the visit block above: every callback prefix is
    # written out inside its OWN f-string and every id is hoisted into a local,
    # because the static guard's scraper stops at the first quote and
    # `_materialize_literal` rewrites `{...}` to `1`.

    @staticmethod
    def _tryout_cancel_row(language: str) -> List[InlineKeyboardButton]:
        """The way out of the try-out, on every screen inside one.

        Unlike a visit, a try-out writes NOTHING until Confirm, so leaving is
        simply leaving -- `staff_back_to_main` is the conversation's own
        fallback and needs no server call.
        """
        return [InlineKeyboardButton(
            f"❌ {i18n.get('staff.cancel', language)}",
            callback_data="staff_back_to_main",
        )]

    @staticmethod
    def tryout_products(language: str, rows: List[Dict]) -> InlineKeyboardMarkup:
        """One button per lendable product, labelled with what is going out.

        `rows` are `{product_id, product_name, quantity}`. A quantity of 0
        prints NOTHING beside the name: a line nobody has set is not a line set
        to zero, and that difference is exactly what *Continue* gates on -- an
        empty basket is not a try-out, and posting one is the 400 this screen
        exists to prevent.
        """
        keyboard = []
        chosen = False
        for row in rows:
            product_id = row.get('product_id')
            quantity = int(row.get('quantity') or 0)
            label = row.get('product_name') or ''
            if quantity:
                chosen = True
                label = f"{label} · {quantity}"
            keyboard.append([InlineKeyboardButton(
                label[:LABEL_MAX],
                callback_data=f"staff_sales_t_p_{product_id}",
            )])
        if chosen:
            keyboard.append([InlineKeyboardButton(
                f"✅ {i18n.get('staff.sales.tryout.done', language)}",
                callback_data="staff_sales_t_done",
            )])
        keyboard.append(SalesKeyboards._tryout_cancel_row(language))
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def tryout_quantity(language: str, product_id: int, current: Optional[int]) -> InlineKeyboardMarkup:
        """One product's try-out quantity. `0` takes the line back off."""
        buttons = [
            InlineKeyboardButton(
                f"• {step}" if current is not None and step == current else str(step),
                callback_data=f"staff_sales_t_qty_{product_id}_{step}",
            )
            for step in TRYOUT_QTY_CHOICES
        ]
        rows = SalesKeyboards._grid_rows(buttons)
        rows.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.sales.visit.back', language)}",
            callback_data="staff_sales_t_back",
        )])
        rows.append(SalesKeyboards._tryout_cancel_row(language))
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def tryout_notes(language: str) -> InlineKeyboardMarkup:
        """Skip the optional note.

        Reuses `staff.sales.new.skip`, exactly as `visit_skip` does: the same
        word on the same kind of button, and a second key would be a second
        thing to translate.
        """
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"⏭ {i18n.get('staff.sales.new.skip', language)}",
                callback_data="staff_sales_t_skip",
            )],
            SalesKeyboards._tryout_cancel_row(language),
        ])

    @staticmethod
    def tryout_confirm(language: str) -> InlineKeyboardMarkup:
        """The last screen before `POST /outlets/<id>/tryouts`.

        *Back* returns to the basket, which is the only screen a quantity can
        be changed on -- and the screen an items refusal lands back on.
        """
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"✅ {i18n.get('staff.sales.tryout.confirm', language)}",
                    callback_data="staff_sales_t_confirm",
                ),
                InlineKeyboardButton(
                    f"⬅️ {i18n.get('staff.sales.visit.back', language)}",
                    callback_data="staff_sales_t_back",
                ),
            ],
            SalesKeyboards._tryout_cancel_row(language),
        ])

    @staticmethod
    def tryout_outlet_link(language: str, outlet_id: int) -> InlineKeyboardMarkup:
        """Back to the outlet card, built ONCE for the two screens that end here.

        The NO-PHONE screen lands on it because the contact is edited THERE
        and nowhere else, and the receipt falls back to it if a 201 ever
        arrives without its `outlet` block (Task 5 publishes one on every
        success, so the receipt normally draws the card itself). Both paths END
        the conversation first, which is what lets this borrow the hub's own
        `staff_sales_outlet_<id>` handler instead of registering a second one.
        """
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            f"📍 {i18n.get('staff.sales.tryout.open_outlet', language)}",
            callback_data=f"staff_sales_outlet_{outlet_id}",
        )]])

    # ---- phase 3: "My stats" ---------------------------------------------------

    @staticmethod
    def stats(language: str, period: str) -> InlineKeyboardMarkup:
        """The period switch under the KPI card.

        The CURRENT window keeps its button and wears a tick rather than being
        dropped: a row that changes shape under the agent's thumb moves the
        other two buttons, and the next tap lands on a window they did not
        mean. One row of three, because the route answers ONE window per
        request -- the card never holds two, so there is nothing to collapse.
        """
        row = []
        for choice in STATS_PERIODS:
            label = i18n.get(f'staff.sales.stats.period_{choice}', language)
            row.append(InlineKeyboardButton(
                f"✅ {label}" if choice == period else label,
                callback_data=f"staff_sales_stats_{choice}",
            ))
        return InlineKeyboardMarkup([row, SalesKeyboards._back_to_main(language)])
