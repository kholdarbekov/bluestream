"""Customer-bot checkout copy must name the RIGHT cap.

Plan 2b gave the COD cap two arms: a customer is blocked either because their
own linked cluster is at ``COD_ACTIVE_DEBT_LIMIT`` (``restriction_scope ==
'person'``) or because the grouped workplace they are shipping to is
(``'place'`` — a coworker's unpaid orders). Telling a customer with a clean
personal record "you have N unpaid orders" when a colleague's debt caused the
block is simply wrong, so ``_cod_restriction_notice`` must branch on the
discriminator.

Pure-function tests over the static method — no Telegram network, no DB.
"""

import pathlib
import sys

import pytest

# telegram_bot modules use workdir-relative BARE imports
# (`from i18n import i18n`, `from api_client import api_client`, ...), so they
# are NOT importable as `telegram_bot.handlers.orders` from tests/unit.
# Same pattern as tests/unit/test_customer_bot_bottles_place.py:19.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "telegram_bot"))

from handlers.orders import OrderHandlers  # noqa: E402
from i18n import Translation  # noqa: E402

PLACE_KEY = "telegram.orders.cod_restricted_place"
PERSON_KEY = "telegram.orders.cod_restricted_person"
HAS_DEBTS_KEY = "telegram.orders.cod_restricted_has_debts"
UNAVAILABLE_KEY = "telegram.orders.cod_restricted_unavailable"

# The two keys that predate 2b and ship with the base seed
# (scripts/seed_prepayment_translations.py), i.e. the guaranteed-present copy.
BASE_SEED = {
    lang: {
        HAS_DEBTS_KEY: f"[{lang}] {{active_debt_count}} ta to'lanmagan buyurtma",
        UNAVAILABLE_KEY: f"[{lang}] naqd to'lov mavjud emas",
    }
    for lang in ("en", "uz", "ru")
}


def _real_i18n(monkeypatch, catalog):
    """Point the handler at the REAL ``telegram_bot`` i18n over ``catalog``.

    Not the echo stub above: this exercise is precisely about what ``i18n.get``
    does to an UNSEEDED key, so the missing-key path must be the production one.
    """
    instance = Translation()
    instance.translations = {lang: dict(rows) for lang, rows in catalog.items()}
    monkeypatch.setattr("handlers.orders.i18n", instance)
    return instance


@pytest.fixture
def captured_i18n(monkeypatch):
    """Stub ``i18n.get`` to echo the key and record its kwargs.

    telegram_bot's ``i18n.get`` does NOT fall back to the key string: on a
    missing key it returns the humanised LAST segment ('Cod restricted place')
    and then silently DROPS every kwarg (telegram_bot/i18n.py:82-93). In an
    unseeded test process that would make key/interpolation assertions fail
    even against correct code, so the stub is mandatory here — and it also
    keeps this task independent of the Task 15 translation seed.
    """
    calls = []

    def fake_get(key, language=None, *args, **kwargs):
        calls.append({"key": key, "language": language, "kwargs": kwargs})
        return key

    monkeypatch.setattr("handlers.orders.i18n.get", fake_get)
    return calls


@pytest.mark.unit
def test_place_scope_uses_place_message(captured_i18n):
    """A place-capped checkout blames the workplace, not the customer."""
    notice = OrderHandlers._cod_restriction_notice(
        {
            "cod_restricted": True,
            "restriction_scope": "place",
            # Clean personal record: the block is entirely a coworker's doing.
            "active_cod_debt_count": 0,
            "place_active_cod_debt_count": 2,
        },
        "en",
    )

    assert notice == "telegram.orders.cod_restricted_place"
    assert captured_i18n == [
        {
            "key": "telegram.orders.cod_restricted_place",
            "language": "en",
            "kwargs": {"place_active_cod_debt_count": 2},
        }
    ]


@pytest.mark.unit
def test_place_scope_wins_even_when_customer_also_has_debts(captured_i18n):
    """Scope is the discriminator, not the presence of a personal count.

    ``get_cod_restriction_context`` evaluates the person arm FIRST
    (business_app/services/cash_collection_service.py:543-548), so a payload
    that says 'place' means the person arm did NOT fire — a non-zero personal
    count is under the limit and must not hijack the message.
    """
    notice = OrderHandlers._cod_restriction_notice(
        {
            "cod_restricted": True,
            "restriction_scope": "place",
            "active_cod_debt_count": 1,
            "place_active_cod_debt_count": 3,
        },
        "uz",
    )

    assert notice == "telegram.orders.cod_restricted_place"
    assert captured_i18n[0]["kwargs"] == {"place_active_cod_debt_count": 3}
    assert captured_i18n[0]["language"] == "uz"


@pytest.mark.unit
def test_place_scope_without_count_degrades_to_zero(captured_i18n):
    """A missing/None place count must not crash or leak 'None' into the copy."""
    notice = OrderHandlers._cod_restriction_notice(
        {"cod_restricted": True, "restriction_scope": "place"}, "ru"
    )

    assert notice == "telegram.orders.cod_restricted_place"
    assert captured_i18n[0]["kwargs"] == {"place_active_cod_debt_count": 0}


@pytest.mark.unit
def test_person_scope_keeps_existing_message(captured_i18n):
    """The person arm is unchanged from today's behaviour."""
    notice = OrderHandlers._cod_restriction_notice(
        {
            "cod_restricted": True,
            "restriction_scope": "person",
            "active_cod_debt_count": 2,
        },
        "en",
    )

    assert notice == "telegram.orders.cod_restricted_has_debts"
    assert captured_i18n[0]["kwargs"] == {"active_debt_count": 2}


@pytest.mark.unit
def test_person_and_place_notices_differ(captured_i18n):
    """The whole point of the task: the two arms must not read the same."""
    place_notice = OrderHandlers._cod_restriction_notice(
        {
            "cod_restricted": True,
            "restriction_scope": "place",
            "active_cod_debt_count": 0,
            "place_active_cod_debt_count": 2,
        },
        "en",
    )
    person_notice = OrderHandlers._cod_restriction_notice(
        {
            "cod_restricted": True,
            "restriction_scope": "person",
            "active_cod_debt_count": 2,
        },
        "en",
    )

    assert place_notice != person_notice
    # ...and each must be the key for ITS arm, not merely "some other string":
    # before this task both scopes fell through to the count-based branch, so a
    # bare inequality would have passed on the pre-change code (place -> the
    # zero-count `cod_restricted_unavailable`, person -> `cod_restricted_has_debts`).
    assert place_notice == "telegram.orders.cod_restricted_place"
    assert person_notice == "telegram.orders.cod_restricted_has_debts"


@pytest.mark.unit
def test_person_scope_states_the_actionable_amount_when_seeded(monkeypatch):
    """Task 30A: a genuine person-arm restriction is the customer's OWN
    money, so — unlike the place arm, which must stay on the count — the
    actionable balance is safe (and useful) to state directly. The count
    alone is not actionable: a customer cannot "reduce" a count, only pay
    down money."""
    from shared.business_config import COD_DEBT_AMOUNT_THRESHOLD

    catalog = {lang: dict(rows) for lang, rows in BASE_SEED.items()}
    for lang, rows in catalog.items():
        rows[PERSON_KEY] = f"[{lang}] balance {{net_debt_total}} over {{threshold}}"

    _real_i18n(monkeypatch, catalog)

    notice = OrderHandlers._cod_restriction_notice(
        {
            "cod_restricted": True,
            "restriction_scope": "person",
            "active_cod_debt_count": 2,
            "cluster_net_open_cod_debt_total": 12000.0,
        },
        "en",
    )

    assert notice == "[en] balance {} over {}".format(
        f"{12000.0:,.0f}", f"{COD_DEBT_AMOUNT_THRESHOLD:,.0f}"
    )


@pytest.mark.unit
def test_an_unseeded_person_key_degrades_to_the_count_based_fallback(monkeypatch):
    """Same unseeded-key guard as the place key above: `cod_restricted_person`
    is NEW, so an environment where its seed has not run must never leak raw
    English — degrade to the always-seeded count-based copy instead."""
    _real_i18n(monkeypatch, BASE_SEED)

    notice = OrderHandlers._cod_restriction_notice(
        {
            "cod_restricted": True,
            "restriction_scope": "person",
            "active_cod_debt_count": 2,
            "cluster_net_open_cod_debt_total": 12000.0,
        },
        "en",
    )

    assert notice == BASE_SEED["en"][HAS_DEBTS_KEY].format(active_debt_count=2)
    assert notice != Translation.humanised_missing_key(PERSON_KEY)
    assert "cod_restricted_person" not in notice


@pytest.mark.unit
def test_missing_scope_falls_back_like_today(captured_i18n):
    """Unlinked + ungrouped baseline: payloads without a scope are untouched.

    ``restriction_scope`` is None whenever the cap did not fire OR the payload
    predates 2b, so the legacy count-based branch stays the fallback.
    """
    assert (
        OrderHandlers._cod_restriction_notice(
            {"cod_restricted": True, "active_cod_debt_count": 1}, "en"
        )
        == "telegram.orders.cod_restricted_has_debts"
    )
    assert (
        OrderHandlers._cod_restriction_notice({"cod_restricted": True}, "en")
        == "telegram.orders.cod_restricted_unavailable"
    )
    # The bare-unavailable branch takes no interpolation kwargs.
    assert captured_i18n[-1]["kwargs"] == {}


@pytest.mark.unit
@pytest.mark.parametrize("language", ["en", "uz", "ru"])
@pytest.mark.parametrize(
    "payload,expected_key",
    [
        # Clean personal record -> the neutral copy; it blames nobody, which is
        # the whole point of the place arm.
        ({"place_active_cod_debt_count": 2}, UNAVAILABLE_KEY),
        ({"place_active_cod_debt_count": 2, "active_cod_debt_count": 0}, UNAVAILABLE_KEY),
        # Personal debts exist too -> the count copy is at least true of them.
        ({"place_active_cod_debt_count": 3, "active_cod_debt_count": 1}, HAS_DEBTS_KEY),
    ],
)
def test_an_unseeded_place_key_never_reaches_a_customer_as_raw_text(
    monkeypatch, language, payload, expected_key
):
    """P1: the place key has no guaranteed seeding path.

    It is rendered by a code path Task 6 made reachable, but ``git grep
    cod_restricted_place HEAD`` is empty — its seed script is UNTRACKED and the
    only run instruction lives in a gitignored plan file. On any environment
    where that script has not run, ``i18n.get`` returns the humanised key, so
    the customer is shown the literal English string 'Cod restricted place' at
    checkout — in Uzbek and Russian too. Degrade to the base-seeded copy
    instead of shipping debug text.
    """
    _real_i18n(monkeypatch, BASE_SEED)

    notice = OrderHandlers._cod_restriction_notice(
        {"cod_restricted": True, "restriction_scope": "place", **payload}, language
    )

    assert notice == BASE_SEED[language][expected_key].format(
        active_debt_count=payload.get("active_cod_debt_count") or 0
    )
    # ...and, stated independently of which key won, no debug text escaped:
    assert notice != Translation.humanised_missing_key(PLACE_KEY)
    assert "Cod restricted place" not in notice
    assert "cod_restricted" not in notice
    assert "telegram.orders" not in notice


@pytest.mark.unit
@pytest.mark.parametrize("language", ["en", "uz", "ru"])
def test_a_blank_seeded_place_row_also_degrades(monkeypatch, language):
    """A row that exists but is empty is not 'seeded' in any useful sense —
    it would send a zero-length message, which Telegram rejects outright."""
    catalog = {lang: dict(rows) for lang, rows in BASE_SEED.items()}
    for rows in catalog.values():
        rows[PLACE_KEY] = ""

    _real_i18n(monkeypatch, catalog)

    notice = OrderHandlers._cod_restriction_notice(
        {"cod_restricted": True, "restriction_scope": "place",
         "place_active_cod_debt_count": 2},
        language,
    )

    assert notice == BASE_SEED[language][UNAVAILABLE_KEY].format(active_debt_count=0)


@pytest.mark.unit
@pytest.mark.parametrize("language", ["en", "uz", "ru"])
def test_a_seeded_place_key_still_wins_over_the_fallback(monkeypatch, language):
    """The guard must not swallow the correct copy once the seed HAS run —
    otherwise the place arm would be dead code and Task 6 pointless."""
    catalog = {lang: dict(rows) for lang, rows in BASE_SEED.items()}
    for lang, rows in catalog.items():
        rows[PLACE_KEY] = f"[{lang}] ish joyida {{place_active_cod_debt_count}} ta qarz"

    _real_i18n(monkeypatch, catalog)

    notice = OrderHandlers._cod_restriction_notice(
        {"cod_restricted": True, "restriction_scope": "place",
         "place_active_cod_debt_count": 2},
        language,
    )

    assert notice == f"[{language}] ish joyida 2 ta qarz"
    assert notice != BASE_SEED[language][UNAVAILABLE_KEY]


@pytest.mark.unit
def test_the_guard_reads_the_same_formula_get_uses(monkeypatch):
    """SSOT pin: the detector is ``i18n``'s own humanising formula.

    If :meth:`Translation.get` ever changes how it renders a missing key
    without :meth:`humanised_missing_key` following, the guard above would stop
    matching and the raw text would ship again — silently. Assert the two agree
    on the actual key at issue, kwargs and all.
    """
    instance = _real_i18n(monkeypatch, BASE_SEED)

    rendered = instance.get(PLACE_KEY, "uz", place_active_cod_debt_count=2)

    assert rendered == Translation.humanised_missing_key(PLACE_KEY)
    assert rendered == "Cod restricted place"


@pytest.mark.unit
def test_explicit_none_scope_falls_back(captured_i18n):
    """`restriction_scope: None` is what 2b emits for a person-count-only cap
    reached through a legacy call site that passes no delivery address."""
    assert (
        OrderHandlers._cod_restriction_notice(
            {
                "cod_restricted": True,
                "restriction_scope": None,
                "active_cod_debt_count": 2,
                "place_active_cod_debt_count": None,
            },
            "en",
        )
        == "telegram.orders.cod_restricted_has_debts"
    )
