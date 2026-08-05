"""Customer bot /bottles rendering against the place-aware overview payload.

`GET /bottles/my-balances` -> `BottleTrackingService.get_customer_bottle_overview`
returns `{is_linked, balances: [...]}` where each row is one DISTINCT PLACE (the
address group when grouped, else the address). The row's number is
`place_balance`; there is deliberately **no** `balance`, no `place_union_balance`
and no `cluster_total_balance` any more, and `place_members` rows carry NAMES
ONLY (spec decision 4). These are pure-function tests over the module-level
formatters -- no Telegram network, no DB...

...except for the LAST test in section 1, which is the anti-blind-spot guard:
every fixture here is built by the shared factory in
``tests/telegram_bot/helpers.py`` and that guard asserts the factory's keys are a
subset of the real ``get_customer_bottle_overview`` payload. Without it a backend
rename leaves this whole module green while the live screen renders 0 for every
customer -- which is exactly what happened. Build new fixtures through the
factory; a literal dict written inline is invisible to the guard.
"""

import importlib.util
import pathlib
import sys

import pytest

# telegram_bot modules use workdir-relative BARE imports
# (`from api_client import api_client`, `from i18n import i18n`, ...), so they
# are NOT importable as `telegram_bot.handlers.bottles` from tests/unit.
# Same pattern as tests/unit/test_bot_payment_methods.py:1-3.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

import handlers.bottles as bottles_mod  # noqa: E402
from handlers.bottles import _build_balance_lines, _render_ledger_lines  # noqa: E402
from tests.telegram_bot.helpers import (  # noqa: E402
    overview_balance_row,
    overview_payload,
    overview_place_member,
)


@pytest.fixture(autouse=True)
def _stub_i18n(monkeypatch):
    """telegram_bot's i18n.get does NOT fall back to the key string: on a
    missing key it returns the humanised LAST segment ('Member line') and
    silently drops every kwarg (telegram_bot/i18n.py:80-93). Stub it so the
    assertions below can see the key AND the interpolated values in an
    unseeded test process."""
    monkeypatch.setattr(
        bottles_mod.i18n,
        "get",
        lambda key, language=None, *a, **kw: " ".join([key] + [str(v) for v in kw.values()]),
    )


def _load_telegram_seed():
    """The real seeded templates, straight from the seed script's KEYS dict."""
    path = REPO_ROOT / "scripts" / "seed_place_group_telegram_translations.py"
    spec = importlib.util.spec_from_file_location("seed_place_group_telegram_translations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _grouped_overview():
    """One grouped place (a shared office) worth 7 bottles, two members.

    Every other field is deliberately DIGIT-FREE so a stray "7" anywhere in the
    rendered body can only have come from `place_balance`."""
    return overview_payload([
        overview_balance_row(
            44, "Office", 7.0,
            full_address="Baker Street", owner_user_id=31, owner_name="Test User",
            is_grouped=True, place_group_id=9,
            place_members=[
                overview_place_member("Test User", is_own=True),
                overview_place_member("Co Worker"),
            ],
        ),
    ])


def _solo_overview():
    """Regression baseline: unlinked + ungrouped customer (Global Constraint #9)."""
    return overview_payload([
        overview_balance_row(
            12, "Home", 3.5,
            full_address="H", owner_user_id=31, owner_name="Solo",
        ),
    ])


def _linked_overview():
    """A linked customer whose cluster spans three DISTINCT places:
    2.5 + 4 + (-1) = 5.5. The payload carries no server-side total any more."""
    return overview_payload(
        [
            overview_balance_row(
                12, "Home", 2.5,
                full_address="H", owner_user_id=31, owner_name="Alice Member",
            ),
            overview_balance_row(
                44, "Office", 4.0,
                full_address="Baker Street", owner_user_id=31, owner_name="Alice Member",
                is_grouped=True, place_group_id=9,
                place_members=[
                    overview_place_member("Alice Member", is_own=True),
                    overview_place_member("Co Worker"),
                ],
            ),
            overview_balance_row(
                88, "Dacha", -1.0,
                full_address="D", owner_user_id=32, owner_name="AliceTwo Member",
                is_own=False,
            ),
        ],
        is_linked=True,
    )


def _row_lines(lines):
    return [line for line in lines if line.startswith("• ")]


# --------------------------------------------------------------------------- #
# Balance screen body -- place keying
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_grouped_place_prints_its_number_once_on_the_place_total_line():
    """Decision D6: a grouped place has ONE pool, so the number is printed once.

    The row line carries the label only; `place_total` -- fed from
    `place_balance` -- is the headline number."""
    lines = _build_balance_lines(_grouped_overview(), "en")

    row_line = _row_lines(lines)[0]
    assert row_line == "• Office", row_line
    assert "7" not in row_line

    place_line = next(line for line in lines if "telegram.bottles.place_total" in line)
    assert place_line.strip() == "telegram.bottles.place_total 7", place_line

    # The fixture is digit-free apart from the balance, so this pins "once in the
    # whole body" as well as "once attributable to this place".
    assert "\n".join(lines).count("7") == 1


@pytest.mark.unit
def test_grouped_place_lists_members_by_name_only():
    """`place_members` rows carry `member_name`/`is_own` and NOTHING else, so the
    handler must pass `name=` alone -- an extra `balance=` kwarg would render a
    phantom 0 next to every coworker (and, against the seeded name-only
    template, is exactly the drift that leaks a raw `{balance}`)."""
    lines = _build_balance_lines(_grouped_overview(), "en")
    member_lines = [line for line in lines if "telegram.bottles.member_line" in line]

    assert len(member_lines) == 2
    assert member_lines[0].strip() == "telegram.bottles.member_line Test User"
    assert member_lines[1].strip() == "telegram.bottles.member_line Co Worker"


@pytest.mark.unit
def test_ungrouped_place_keeps_its_number_on_the_row_line():
    lines = _build_balance_lines(_solo_overview(), "en")

    assert _row_lines(lines) == ["• Home: <b>3.5</b>"]
    assert not any("telegram.bottles.place_total" in line for line in lines)
    assert not any("telegram.bottles.member_line" in line for line in lines)


@pytest.mark.unit
def test_cluster_total_is_the_client_side_sum_of_place_balances():
    """The overview has no server-side total by design (a shared place's balance
    belongs to the place, not to each member), and its rows are already
    scope-deduped, so the bot sums `place_balance` itself."""
    lines = _build_balance_lines(_linked_overview(), "en")

    total_line = next(line for line in lines if "telegram.bottles.cluster_total" in line)
    assert total_line == "telegram.bottles.cluster_total 5.5", total_line


@pytest.mark.unit
def test_no_cluster_total_for_an_unlinked_customer():
    lines = _build_balance_lines(_solo_overview(), "en")
    assert not any("telegram.bottles.cluster_total" in line for line in lines)


@pytest.mark.unit
def test_balance_lines_use_the_four_place_keys_for_a_grouped_linked_customer():
    text = "\n".join(_build_balance_lines(_linked_overview(), "en"))
    for key in (
        "telegram.bottles.place_total",
        "telegram.bottles.member_line",
        "telegram.bottles.cluster_total",
        "telegram.bottles.linked_account_line",
    ):
        assert key in text, key


@pytest.mark.unit
def test_balance_lines_ungrouped_unlinked_stays_simple():
    text = "\n".join(_build_balance_lines(_solo_overview(), "en"))
    assert "Home" in text and "3.5" in text
    assert "telegram.bottles.place_total" not in text
    assert "telegram.bottles.cluster_total" not in text
    assert "telegram.bottles.member_line" not in text
    assert "telegram.bottles.linked_account_line" not in text


@pytest.mark.unit
def test_balance_lines_own_address_is_not_labelled_with_an_owner():
    """A row the viewer owns keeps the plain title -- the linked-account label is
    only for a sibling account's address."""
    lines = _build_balance_lines(_linked_overview(), "en")
    own_line = next(line for line in lines if "Home" in line)
    assert own_line == "• Home: <b>2.5</b>"

    sibling_line = next(line for line in lines if "Dacha" in line)
    assert "telegram.bottles.linked_account_line" in sibling_line
    assert "AliceTwo Member" in sibling_line


@pytest.mark.unit
def test_balance_lines_render_fractional_and_negative_place_balances():
    lines = _build_balance_lines(_linked_overview(), "en")
    assert any(line == "• Home: <b>2.5</b>" for line in lines)
    sibling_line = next(line for line in lines if "Dacha" in line)
    assert "-1" in sibling_line


@pytest.mark.unit
def test_balance_lines_html_escape_titles_and_names():
    """The body is sent with parse_mode='HTML'; an unescaped '<' in a
    user-controlled title/name makes Telegram reject the whole message."""
    overview = overview_payload(
        [
            overview_balance_row(
                1, "Home <3 & Co", 1.0,
                full_address="H", owner_user_id=12, owner_name="Bob <b>", is_own=False,
                is_grouped=True, place_group_id=4,
                place_members=[overview_place_member("Eve <i>")],
            ),
        ],
        is_linked=True,
    )
    text = "\n".join(_build_balance_lines(overview, "en"))
    assert "Home &lt;3 &amp; Co" in text
    assert "Home <3 & Co" not in text
    assert "Bob &lt;b&gt;" in text and "Bob <b>" not in text
    assert "Eve &lt;i&gt;" in text and "Eve <i>" not in text


@pytest.mark.unit
def test_balance_lines_fall_back_to_full_address_then_id():
    overview = overview_payload([
        overview_balance_row(1, None, 0.0, full_address="Some street 5", owner_name="Solo"),
        overview_balance_row(2, None, 0.0, full_address=None, owner_name="Solo"),
    ])
    text = "\n".join(_build_balance_lines(overview, "en"))
    assert "Some street 5" in text
    assert "#2" in text


@pytest.mark.unit
def test_balance_lines_tolerate_a_missing_balances_key():
    assert _build_balance_lines({}, "en")  # title-only, no crash


@pytest.mark.unit
def test_the_seeded_templates_format_cleanly_with_the_handler_kwargs(monkeypatch):
    """Ordering hazard H1, pinned.

    ``telegram_bot/i18n.py:88-93`` SWALLOWS the ``KeyError`` from
    ``translation.format(**kwargs)`` and returns the RAW template, so a handler
    that stops passing a placeholder the seed still declares reaches the
    customer as a literal ``{balance}``. Render the body through the real seeded
    strings and assert nothing unformatted survives."""
    seed = _load_telegram_seed()

    def _real_templates(key, language=None, *args, **kwargs):
        template = seed.KEYS.get(key, {}).get("en")
        if template is None:
            return key  # not owned by this seed (e.g. telegram.bottles.title)
        try:
            return template.format(**kwargs)
        except KeyError as exc:
            pytest.fail(f"{key} declares {exc} but the handler does not pass it")

    monkeypatch.setattr(bottles_mod.i18n, "get", _real_templates)

    text = "\n".join(_build_balance_lines(_linked_overview(), "en"))
    assert "{" not in text and "}" not in text, text


# --------------------------------------------------------------------------- #
# THE anti-blind-spot guard
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_fabricated_overview_matches_the_real_customer_overview(
    app, db, place, sample_user, second_sample_user
):
    """Everything above feeds a FABRICATED payload to the real formatter, so
    nothing above can notice a backend rename. This derives the real payload once
    and pins the fabrication against it, in KEYS and in VALUE.

    Its absence is why Plan A shipped: both bot test modules kept fabricating
    ``balance`` / ``place_union_balance`` / ``cluster_total_balance`` after the
    service stopped emitting them, stayed green, and the live /bottles screen
    rendered 0 for every customer.
    """
    from decimal import Decimal

    from business_app.services.bottle_tracking_service import BottleTrackingService
    from shared.enums import BottleLedgerEventType

    service = BottleTrackingService()
    # Two coworkers at ONE grouped place: 5 taken at their own door, 2 at the
    # coworker's. The place holds 7 and cannot be sliced into 5/2.
    service._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"),
    )
    service._create_ledger_entry(
        user_id=second_sample_user.id, address_id=place["a2"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("2"),
    )
    db.session.flush()

    real = service.get_customer_bottle_overview(sample_user.id)

    # KEY pin: the shared factory may not invent a field the service never sends.
    assert set(overview_payload([])) <= set(real)
    assert set(overview_balance_row(1, "x", 0.0)) <= set(real["balances"][0])
    assert set(overview_place_member("x")) <= set(real["balances"][0]["place_members"][0])

    # VALUE pin: `place_balance` is the whole POOL at that door (5 + 2), not the
    # viewer's own 5. This is the substance of the entire re-key, and it is what
    # a key-set-only assertion cannot see.
    row = real["balances"][0]
    assert row["place_balance"] == 7.0
    assert row["is_grouped"] is True
    assert row["place_group_id"] == place["group"].id
    assert row["is_own"] is True
    # ...and the coworker reads the SAME pool from their own address.
    coworker = service.get_customer_bottle_overview(second_sample_user.id)
    assert coworker["balances"][0]["place_balance"] == 7.0

    # Members are names only, and BOTH coworkers are listed even though only one
    # of them took a delivery at their own door.
    assert {m["member_name"] for m in row["place_members"]} == {"Test User", "Co Worker"}
    for member in row["place_members"]:
        assert "balance" not in member

    # The keys the bot tests used to fabricate are gone — pin their absence, or a
    # re-introduced alias would let the stale readers pass again.
    assert "cluster_total_balance" not in real
    for stale in ("balance", "place_union_balance", "group_union_balance",
                  "bottle_balance_id", "cluster_total_balance"):
        assert stale not in row, stale

    # The formatter must survive the REAL payload, not just the fabricated one.
    lines = _build_balance_lines(real, "en")
    assert any(line.strip() == "telegram.bottles.place_total 7" for line in lines), lines


# --------------------------------------------------------------------------- #
# Ledger lines: member attribution (place ledger -- unchanged by the re-key)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_ledger_lines_prefix_other_members_name():
    items = [
        {"event_type": "standalone_collection", "order_id": None, "quantity": -2.0,
         "occurred_at": "2026-07-20T10:00:00+00:00", "order_number": None,
         "member_name": "Bob Coworker", "is_own": False},
    ]
    lines = _render_ledger_lines(items, "en")
    assert any("Bob Coworker" in line for line in lines)


@pytest.mark.unit
def test_ledger_lines_do_not_prefix_the_viewers_own_entries():
    items = [
        {"event_type": "standalone_collection", "order_id": None, "quantity": -2.0,
         "occurred_at": "2026-07-20T10:00:00+00:00", "order_number": None,
         "member_name": "Alice Member", "is_own": True},
    ]
    lines = _render_ledger_lines(items, "en")
    assert not any("Alice Member" in line for line in lines)


@pytest.mark.unit
def test_ledger_lines_unchanged_when_the_row_has_no_member_fields():
    """Back-compat: rows without member_name/is_own (ungrouped address, older
    payloads) render exactly as before."""
    items = [
        {"event_type": "standalone_collection", "order_id": None, "quantity": 3,
         "occurred_at": "2026-07-20T10:00:00+00:00", "order_number": None},
    ]
    assert _render_ledger_lines(items, "en") == [
        "telegram.bottles.event.standalone_collection (20.07.2026): 3"
    ]


@pytest.mark.unit
def test_ledger_order_group_line_prefixes_a_foreign_member():
    """A collapsed #order line for a coworker's delivery is attributed too."""
    items = [
        {"event_type": "delivery", "order_id": 77, "quantity": 2,
         "occurred_at": "2026-07-20T10:00:00+00:00", "order_number": "TG_77",
         "member_name": "Bob Coworker", "is_own": False},
        {"event_type": "return_on_delivery", "order_id": 77, "quantity": -2,
         "occurred_at": "2026-07-20T10:00:00+00:00", "order_number": "TG_77",
         "member_name": "Bob Coworker", "is_own": False},
    ]
    lines = _render_ledger_lines(items, "en")
    assert len(lines) == 1
    assert lines[0].startswith("Bob Coworker")
    assert "#TG_77" in lines[0]


@pytest.mark.unit
def test_ledger_order_group_line_not_prefixed_for_own_order():
    items = [
        {"event_type": "delivery", "order_id": 77, "quantity": 2,
         "occurred_at": "2026-07-20T10:00:00+00:00", "order_number": "TG_77",
         "member_name": "Alice Member", "is_own": True},
    ]
    lines = _render_ledger_lines(items, "en")
    assert len(lines) == 1
    assert lines[0].startswith("#TG_77")


@pytest.mark.unit
def test_ledger_member_name_is_html_escaped():
    items = [
        {"event_type": "standalone_collection", "order_id": None, "quantity": 1,
         "occurred_at": "2026-07-20T10:00:00+00:00", "order_number": None,
         "member_name": "Bob <b>", "is_own": False},
    ]
    line = _render_ledger_lines(items, "en")[0]
    assert "Bob &lt;b&gt;" in line
    assert "Bob <b>" not in line
