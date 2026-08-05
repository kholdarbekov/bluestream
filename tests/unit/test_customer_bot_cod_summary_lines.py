"""Customer-bot wallet block — ``_build_cod_summary_lines`` (plan 2c Task 16).

The MONEY counterpart of ``handlers/bottles.py::_build_balance_lines``: at the
bottom of the orders menu a linked customer sees their cluster's unpaid COD
total as ONE customer, and every grouped workplace shows that place's unified
open COD total with a per-order breakdown naming the coworker each order belongs
to (approved full in-group transparency, spec §7).

Pure-function tests over a module-level formatter — no Telegram, no network,
no DB.

Two environment landmines this file works around, both of which silently make
assertions meaningless rather than failing loudly:

* ``telegram_bot`` modules use workdir-relative BARE imports (``from i18n import
  i18n``), so they are NOT importable as ``telegram_bot.handlers.orders``; the
  package directory has to go on ``sys.path`` and the BARE module path is what
  ``monkeypatch`` must target.
* ``i18n.get`` does NOT fall back to the key. On a missing key it returns the
  humanised last segment ('Cluster debt total') and then ``.format()`` silently
  DROPS every kwarg — so in an unseeded test process an assertion on a rendered
  name or number would pass against broken code. The stub is mandatory.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "telegram_bot"))

import handlers.orders as orders_mod  # noqa: E402
from handlers.orders import _build_cod_summary_lines, _cod_summary_payload  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_i18n(monkeypatch):
    """Echo the key plus every interpolated value, so a dropped placeholder or
    a wrong key is visible in the rendered text."""
    monkeypatch.setattr(
        orders_mod.i18n,
        "get",
        lambda key, language=None, *a, **kw: " ".join([key] + [str(v) for v in kw.values()]),
    )


def _summary(**overrides):
    base = {
        "cluster_member_count": 2,
        "cluster_delivered_outstanding_amount": 30000.0,
        "available_prepayment_balance": 0.0,
        "places": [
            {
                "place_group_id": 7,
                "label": "Acme office",
                "place_open_cod_debt_total": 35000.0,
                "items": [
                    {"order_number": "ORD-1", "member_name": "Alice Member",
                     "outstanding_amount": 15000.0, "created_at": None},
                    {"order_number": "ORD-2", "member_name": "Bob Coworker",
                     "outstanding_amount": 20000.0, "created_at": None},
                ],
            }
        ],
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_place_and_cluster_lines_render_with_member_names():
    text = "\n".join(_build_cod_summary_lines(_summary(), "en"))
    assert "telegram.payments.cluster_debt_total" in text
    assert "Acme office" in text
    assert "Alice Member" in text and "Bob Coworker" in text
    assert "ORD-2" in text


@pytest.mark.unit
def test_unlinked_ungrouped_renders_nothing():
    summary = {"cluster_member_count": 1, "cluster_delivered_outstanding_amount": 0.0,
               "available_prepayment_balance": 0.0, "places": []}
    assert _build_cod_summary_lines(summary, "en") == []


@pytest.mark.unit
def test_solo_customer_with_prepaid_credit_and_debt_still_renders_nothing():
    """Regression baseline (global constraint): an unlinked + ungrouped customer's
    orders menu must stay byte-identical to today, debt or credit notwithstanding."""
    summary = {"cluster_member_count": 1, "cluster_delivered_outstanding_amount": 42000.0,
               "available_prepayment_balance": 9000.0, "places": []}
    assert _build_cod_summary_lines(summary, "en") == []


@pytest.mark.unit
def test_missing_and_empty_payload_render_nothing():
    """A degraded/empty API payload must not raise inside the menu handler."""
    assert _build_cod_summary_lines({}, "en") == []
    assert _build_cod_summary_lines({"places": None, "cluster_member_count": None}, "en") == []


@pytest.mark.unit
def test_cluster_line_carries_the_formatted_cluster_total():
    lines = _build_cod_summary_lines(_summary(places=[]), "en")
    assert lines == ["telegram.payments.cluster_debt_total 30,000"]


@pytest.mark.unit
def test_linked_customer_sees_cluster_wide_prepaid_credit():
    """Spec §7 promises debt AND prepaid credit as one customer. The value is
    already cluster-wide (2b); it must actually reach the screen."""
    lines = _build_cod_summary_lines(
        _summary(places=[], available_prepayment_balance=12500.0), "en")
    assert lines == [
        "telegram.payments.cluster_debt_total 30,000",
        "telegram.orders.cod_prepaid_balance 12,500",
    ]


@pytest.mark.unit
def test_zero_prepaid_credit_adds_no_line():
    assert _build_cod_summary_lines(_summary(places=[]), "en") == [
        "telegram.payments.cluster_debt_total 30,000",
    ]


@pytest.mark.unit
def test_grouped_but_unlinked_customer_gets_the_place_block_without_a_cluster_line():
    """A solo customer at a shared workplace is the common case: no cluster
    line (they are one account), but the place block is the whole point."""
    lines = _build_cod_summary_lines(_summary(cluster_member_count=1), "en")
    assert not any("cluster_debt_total" in ln for ln in lines)
    assert lines[0] == "telegram.payments.place_debt_total Acme office 35,000"
    assert lines[1].strip() == "telegram.payments.place_order_line ORD-1 Alice Member 15,000"
    assert lines[2].strip() == "telegram.payments.place_order_line ORD-2 Bob Coworker 20,000"


@pytest.mark.unit
def test_place_without_a_label_falls_back_to_its_id():
    lines = _build_cod_summary_lines(
        _summary(cluster_member_count=1,
                 places=[{"place_group_id": 7, "label": None,
                          "place_open_cod_debt_total": 0.0, "items": []}]),
        "en",
    )
    assert lines == ["telegram.payments.place_debt_total #7 0"]


@pytest.mark.unit
def test_item_fields_are_never_rendered_as_none():
    lines = _build_cod_summary_lines(
        _summary(cluster_member_count=1,
                 places=[{"place_group_id": 7, "label": "Acme office",
                          "place_open_cod_debt_total": 15000.0,
                          "items": [{"order_number": None, "member_name": None,
                                     "outstanding_amount": None, "created_at": None}]}]),
        "en",
    )
    assert "None" not in "\n".join(lines)
    assert lines[1].strip() == "telegram.payments.place_order_line — — 0"


@pytest.mark.unit
def test_names_are_not_html_escaped_because_the_menu_is_plain_text():
    """``orders_menu`` sends its body with NO ``parse_mode`` and the bot sets no
    ``Defaults(parse_mode=...)``, so the text is plain. Escaping here would print
    ``O&#x27;Brien`` — and apostrophes are ordinary in Uzbek names and labels."""
    lines = _build_cod_summary_lines(
        _summary(cluster_member_count=1,
                 places=[{"place_group_id": 7, "label": "O'Brien & Co",
                          "place_open_cod_debt_total": 1000.0,
                          "items": [{"order_number": "ORD-1", "member_name": "G'ulom O'g'li",
                                     "outstanding_amount": 1000.0, "created_at": None}]}]),
        "en",
    )
    text = "\n".join(lines)
    assert "O'Brien & Co" in text
    assert "G'ulom O'g'li" in text
    assert "&#x27;" not in text and "&amp;" not in text


@pytest.mark.unit
def test_per_order_breakdown_is_capped_but_the_place_total_is_not():
    """The breakdown is capped so one busy office cannot blow the 4096-char
    Telegram message limit; the TOTAL above it stays complete, so the customer
    is never misled about the amount."""
    items = [{"order_number": f"ORD-{i}", "member_name": f"Member {i}",
              "outstanding_amount": 1000.0, "created_at": None} for i in range(25)]
    lines = _build_cod_summary_lines(
        _summary(cluster_member_count=1,
                 places=[{"place_group_id": 7, "label": "Acme office",
                          "place_open_cod_debt_total": 25000.0, "items": items}]),
        "en",
    )
    assert lines[0] == "telegram.payments.place_debt_total Acme office 25,000"
    assert len(lines) == 1 + 10
    assert "ORD-9" in lines[-1]


@pytest.mark.unit
def test_multiple_places_each_get_their_own_block():
    lines = _build_cod_summary_lines(
        _summary(cluster_member_count=1,
                 places=[
                     {"place_group_id": 7, "label": "Office A",
                      "place_open_cod_debt_total": 1000.0, "items": []},
                     {"place_group_id": 8, "label": "Office B",
                      "place_open_cod_debt_total": 2000.0, "items": []},
                 ]),
        "en",
    )
    assert lines == [
        "telegram.payments.place_debt_total Office A 1,000",
        "telegram.payments.place_debt_total Office B 2,000",
    ]


class _Resp:
    def __init__(self, success, data):
        self.success = success
        self.data = data


@pytest.mark.unit
def test_payload_is_unwrapped_from_the_success_envelope():
    """``APIResponse.data`` is the whole ``{'success': …, 'data': {…}}`` envelope.
    Handing the envelope straight to the formatter would render nothing at all
    and look like "the customer simply has no debt"."""
    resp = _Resp(True, {"success": True, "data": {"cluster_member_count": 2,
                                                  "cluster_delivered_outstanding_amount": 5000.0,
                                                  "available_prepayment_balance": 0.0,
                                                  "places": []}})
    assert _cod_summary_payload(resp)["cluster_member_count"] == 2
    assert _build_cod_summary_lines(_cod_summary_payload(resp), "en") == [
        "telegram.payments.cluster_debt_total 5,000",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("response", [
    None,
    _Resp(False, None),
    _Resp(False, {"data": {"cluster_member_count": 9}}),   # failure wins over a body
    _Resp(True, None),
    _Resp(True, "not-a-dict"),
    _Resp(True, {}),
    _Resp(True, {"data": None}),
    _Resp(True, {"data": []}),
])
def test_a_failed_or_misshapen_response_renders_nothing(response):
    """The orders menu must survive every degraded answer — the customer asked
    for their orders, not for this block."""
    assert _cod_summary_payload(response) == {}
    assert _build_cod_summary_lines(_cod_summary_payload(response), "en") == []


@pytest.mark.unit
def test_only_the_three_task15_keys_plus_the_existing_prepaid_key_are_used(monkeypatch):
    """Pins the key set: an unseeded key renders the humanised last segment with
    every placeholder silently dropped, which looks like a formatting bug in
    production rather than a missing translation."""
    used = []
    monkeypatch.setattr(orders_mod.i18n, "get",
                        lambda key, language=None, *a, **kw: used.append(key) or key)
    _build_cod_summary_lines(_summary(available_prepayment_balance=5000.0), "en")
    assert set(used) == {
        "telegram.payments.cluster_debt_total",
        "telegram.payments.place_debt_total",
        "telegram.payments.place_order_line",
        "telegram.orders.cod_prepaid_balance",
    }
