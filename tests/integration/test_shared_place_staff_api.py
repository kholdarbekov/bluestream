"""The driver's three shared-place calls (spec 2026-11-10 §5, §9), driven through the staff HTTP routes.

Cases are built the way detection builds them (frozen candidates, asked driver); the Celery publish and Slack are
recorded with the signature-binding fixtures of test_shared_place_alerts.py.
"""

from datetime import UTC, datetime

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token

from business_app.models.shared_place_case import SharedPlaceCase
from business_app.tasks import customer_link_tasks
from tests.integration.shared_place_builders import BASE_LAT, BASE_LNG, customer, driver, pinned_address, stop_for
from tests.integration.test_shared_place_alerts import published, slack  # noqa: F401 - fixtures

pytestmark = pytest.mark.integration

QUESTION = "/api/v1/staff/delivery/{}/shared-place-question"
ANSWER = "/api/v1/staff/shared-place-cases/{}/answer"
NOTE = "/api/v1/staff/shared-place-cases/{}/note"
CANDIDATE = {"ref": {"kind": "address", "id": 99999}, "first_name": "Akmal", "empties": 8, "distance_m": 14}
SECOND = {"ref": {"kind": "group", "id": 99998}, "first_name": "Sardor", "empties": 5, "distance_m": 31}


def _bearer(user):
    token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _case(db, *, first="Jasur"):
    """An awaiting_driver case for a fresh delivery; returns (case, driver, delivery)."""
    u = customer(db, first)
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=1)
    case = SharedPlaceCase(
        order_id=order.id,
        delivery_id=delivery.id,
        address_id=address.id,
        returned_qty=3,
        place_balance_before=0,
        asked_driver_id=drv.id,
        candidates=[CANDIDATE, SECOND],
        status="awaiting_driver",
    )
    db.session.add(case)
    db.session.commit()
    return case, drv, delivery


def _put(url, drv, body):
    return current_app.test_client().put(url, json=body, headers=_bearer(drv))


def _reload(db, case):
    db.session.expire_all()
    return SharedPlaceCase.query.get(case.id)


def _error_code(response):
    return response.get_json()["error_code"]


def test_question_asks_the_asked_driver(db):
    case, drv, delivery = _case(db)

    response = current_app.test_client().get(QUESTION.format(delivery.id), headers=_bearer(drv))

    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "ask": True,
        "case_id": case.id,
        "customer_first_name": "Jasur",
        "candidates": [
            {"index": 0, "first_name": "Akmal", "empties": 8, "distance_m": 14},
            {"index": 1, "first_name": "Sardor", "empties": 5, "distance_m": 31},
        ],
    }


def test_question_is_silent_for_other_driver_after_answer_and_without_case(db, published):
    case, drv, delivery = _case(db)
    other = driver(db, "Olim")
    client = current_app.test_client()

    assert client.get(QUESTION.format(delivery.id), headers=_bearer(other)).get_json()["data"] == {"ask": False}

    assert _put(ANSWER.format(case.id), drv, {"answer": "skipped"}).status_code == 200
    assert client.get(QUESTION.format(delivery.id), headers=_bearer(drv)).get_json()["data"] == {"ask": False}

    assert client.get(QUESTION.format(delivery.id + 1000), headers=_bearer(drv)).get_json()["data"] == {"ask": False}


def test_question_needs_a_driver_token(db, admin_claim_headers):
    _, _, delivery = _case(db)

    response = current_app.test_client().get(QUESTION.format(delivery.id), headers=admin_claim_headers)

    assert response.status_code == 403


def test_answer_pick_moves_case_to_admin_and_queues_alert(db, published):
    case, drv, _ = _case(db)

    response = _put(ANSWER.format(case.id), drv, {"answer": "picked", "candidate_index": 0})

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"] == {
        "case_id": case.id,
        "answer": "picked",
        "picked_first_name": "Akmal",
        "note_saved": False,
    }
    row = _reload(db, case)
    assert (row.status, row.driver_answer, row.picked_index, row.driver_note) == ("awaiting_admin", "picked", 0, None)
    assert row.answered_at is not None
    assert published == [((case.id,), {}, {"countdown": 0})]


def test_none_with_note_in_one_put(db, published):
    case, drv, _ = _case(db)

    response = _put(ANSWER.format(case.id), drv, {"answer": "none", "note": "  lives with his aunt  "})

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"] == {
        "case_id": case.id,
        "answer": "none",
        "picked_first_name": None,
        "note_saved": True,
    }
    row = _reload(db, case)
    assert (row.driver_answer, row.picked_index, row.driver_note) == ("none", None, "lives with his aunt")


def test_same_answer_again_is_a_no_op(db, published):
    case, drv, _ = _case(db)
    first = _put(ANSWER.format(case.id), drv, {"answer": "picked", "candidate_index": 1, "note": "next door"})
    stamp = _reload(db, case).answered_at

    again = _put(ANSWER.format(case.id), drv, {"answer": "picked", "candidate_index": 1, "note": "next door"})
    no_note = _put(ANSWER.format(case.id), drv, {"answer": "picked", "candidate_index": 1})

    assert first.status_code == again.status_code == no_note.status_code == 200
    assert again.get_json()["data"] == first.get_json()["data"] == no_note.get_json()["data"]
    assert first.get_json()["data"]["picked_first_name"] == "Sardor"
    assert _reload(db, case).answered_at == stamp
    assert published == [((case.id,), {}, {"countdown": 0})]


def test_different_answer_is_409(db, published):
    case, drv, _ = _case(db)
    assert _put(ANSWER.format(case.id), drv, {"answer": "picked", "candidate_index": 0}).status_code == 200

    for body in (
        {"answer": "none"},
        {"answer": "picked", "candidate_index": 1},
        {"answer": "picked", "candidate_index": 0, "note": "a new note"},
    ):
        response = _put(ANSWER.format(case.id), drv, body)
        assert response.status_code == 409, body
        assert _error_code(response) == "SHARED_PLACE_ANSWER_REFUSED"

    row = _reload(db, case)
    assert (row.driver_answer, row.picked_index, row.driver_note) == ("picked", 0, None)
    assert len(published) == 1


def test_answer_after_resolution_is_409(db, published):
    case, drv, _ = _case(db)
    case.status = "closed"
    case.decided_at = datetime.now(UTC)
    db.session.commit()

    response = _put(ANSWER.format(case.id), drv, {"answer": "picked", "candidate_index": 0})

    assert response.status_code == 409
    assert _error_code(response) == "SHARED_PLACE_ANSWER_REFUSED"
    assert _reload(db, case).driver_answer is None
    assert published == []


@pytest.mark.parametrize(
    "body",
    [
        {"answer": "picked"},
        {"answer": "picked", "candidate_index": 2},
        {"answer": "picked", "candidate_index": -1},
        {"answer": "picked", "candidate_index": "0"},
        {"answer": "none", "candidate_index": 0},
        {"answer": "skipped", "candidate_index": 0},
        {"answer": "skipped", "note": "x"},
        {"answer": "maybe"},
        {"answer": "none", "note": "   "},
        {"answer": "none", "note": "x" * 501},
        {"answer": "none", "extra": 1},
        {},
    ],
)
def test_bad_index_and_skip_with_note_are_400(db, published, body):
    case, drv, _ = _case(db)

    response = _put(ANSWER.format(case.id), drv, body)

    assert response.status_code == 400, body
    assert _error_code(response) == "SHARED_PLACE_ANSWER_INVALID"
    assert _reload(db, case).driver_answer is None
    assert published == []


def test_note_is_write_once(db, published):
    case, drv, _ = _case(db)
    assert _put(ANSWER.format(case.id), drv, {"answer": "none"}).status_code == 200

    first = _put(NOTE.format(case.id), drv, {"note": " shares a flat "})
    same = _put(NOTE.format(case.id), drv, {"note": "shares a flat"})
    other = _put(NOTE.format(case.id), drv, {"note": "something else"})

    assert first.status_code == same.status_code == 200
    assert first.get_json()["data"] == same.get_json()["data"] == {"case_id": case.id, "note_saved": True}
    assert other.status_code == 409
    assert _error_code(other) == "SHARED_PLACE_ANSWER_REFUSED"
    assert _reload(db, case).driver_note == "shares a flat"
    assert len(published) == 1  # the answer's alert only; a note queues nothing


def test_note_then_answer_keeps_the_note(db, published):
    """Write-once (spec §4.2, §4.4): a note stored before the answer survives an answer that carries none, an answer
    that repeats it is accepted, and an answer with a different note is refused and changes nothing."""
    case, drv, _ = _case(db)
    other, other_drv, _ = _case(db, first="Bobur")
    third, third_drv, _ = _case(db, first="Sardor")
    for row, asked, note in (
        (case, drv, "neighbour on floor 3 owns these"),
        (other, other_drv, "gate code 12"),
        (third, third_drv, "first words"),
    ):
        assert _put(NOTE.format(row.id), asked, {"note": note}).status_code == 200

    answered = _put(ANSWER.format(case.id), drv, {"answer": "picked", "candidate_index": 0})
    repeated = _put(ANSWER.format(other.id), other_drv, {"answer": "none", "note": " gate code 12 "})
    refused = _put(ANSWER.format(third.id), third_drv, {"answer": "none", "note": "other words"})

    assert answered.status_code == repeated.status_code == 200
    assert answered.get_json()["data"]["note_saved"] is True
    row = _reload(db, case)
    assert (row.driver_note, row.driver_answer) == ("neighbour on floor 3 owns these", "picked")
    assert _reload(db, other).driver_note == "gate code 12"
    assert refused.status_code == 409
    assert _error_code(refused) == "SHARED_PLACE_ANSWER_REFUSED"
    row = _reload(db, third)
    assert (row.driver_note, row.driver_answer, row.status) == ("first words", None, "awaiting_driver")
    assert published == [((case.id,), {}, {"countdown": 0}), ((other.id,), {}, {"countdown": 0})]


@pytest.mark.parametrize("body", [{"note": "x" * 501}, {"note": ""}, {"note": "  "}, {}, {"note": 5}])
def test_note_over_500_is_400(db, body):
    case, drv, _ = _case(db)

    response = _put(NOTE.format(case.id), drv, body)

    assert response.status_code == 400
    assert _error_code(response) == "SHARED_PLACE_ANSWER_INVALID"
    assert _reload(db, case).driver_note is None


def test_note_of_exactly_500_is_saved(db):
    case, drv, _ = _case(db)

    assert _put(NOTE.format(case.id), drv, {"note": "x" * 500}).status_code == 200
    assert _reload(db, case).driver_note == "x" * 500


def test_note_on_a_settled_case_is_409(db):
    case, drv, _ = _case(db)
    case.status = "resolved"
    case.resolution_kind = "same_home"
    case.decided_at = datetime.now(UTC)
    db.session.commit()

    response = _put(NOTE.format(case.id), drv, {"note": "too late"})

    assert response.status_code == 409
    assert _error_code(response) == "SHARED_PLACE_ANSWER_REFUSED"


def test_other_driver_gets_404(db, published):
    case, _, _ = _case(db)
    other = driver(db, "Olim")

    for url, body in (
        (ANSWER, {"answer": "skipped"}),
        (NOTE, {"note": "hi"}),
    ):
        response = _put(url.format(case.id), other, body)
        assert response.status_code == 404
        assert _error_code(response) == "SHARED_PLACE_CASE_NOT_FOUND"
    assert _put(ANSWER.format(case.id + 1000), other, {"answer": "skipped"}).status_code == 404
    assert _reload(db, case).driver_answer is None
    assert published == []


def test_late_answer_is_accepted_without_a_second_post(db, published, slack):
    case, drv, _ = _case(db)
    case.alert_posted_at = datetime.now(UTC)  # the 15-minute alert already went out
    db.session.commit()

    response = _put(ANSWER.format(case.id), drv, {"answer": "picked", "candidate_index": 0})

    assert response.status_code == 200, response.get_json()
    assert published == [((case.id,), {}, {"countdown": 0})]
    assert customer_link_tasks.post_shared_place_case_alert_task.run(case.id) is False
    assert slack == []
