"""`shared.eta_copy`: the ONE rule for what the customer is told about the arrival.

Honest-ETA spec 2026-11-03 §4.8. `DeliveryEtaService.customer_eta` publishes the stage, the minutes left, the "by" time
on the Tashkent clock and where the minutes came from; these lines are what is said for them. `promised_time_lines` is
the promised time (the in-transit message says it), `track_lines` is the customer bot's Track screen: the promised time
while the driver is on the way, "On the way" when nothing is promised, "Arrived at your address" once the driver
arrived. Driven with the shipped seed rows through production's own renderer (`_seeded`:
`shared.i18n_rendering.render_translation`), and each expectation is the literal sentence a customer reads, so a
drifted seed row fails here.
"""

import pytest

from shared.constants import ETA_ATTRIBUTION_GOOGLE, ETA_STAGE_ARRIVED, ETA_STAGE_ON_THE_WAY
from shared.eta_copy import promised_time_lines, track_lines
from shared.i18n_rendering import humanise_key
from tests.unit.test_eta_copy_is_seeded import _seeded

pytestmark = pytest.mark.unit

ON_THE_WAY = "telegram.orders.eta_on_the_way"
ARRIVING_NOW = "telegram.orders.eta_arriving_now"
ATTRIBUTION = "telegram.orders.eta_google_attribution"
NO_TIME = "telegram.orders.eta_on_the_way_no_time"
ARRIVED = "telegram.orders.eta_arrived"
BY = "14:29"


def _lines(minutes_left, *, attribution=ETA_ATTRIBUTION_GOOGLE, language="en", render=None, by_time=BY):
    return promised_time_lines(
        render or _seeded(language), minutes_left=minutes_left, by_time=by_time, attribution=attribution
    )


# What a renderer that degrades like `render_translation` (the bot's `Translation.renderer`, the backend's) answers for
# copy it cannot render: nothing, or the humanised key (a row missing in every language, or a template its values
# cannot fill).
UNSAYABLE_ANSWERS = [
    pytest.param(lambda key: "", id="nothing"),
    pytest.param(humanise_key, id="the-humanised-key"),
]


def _unable_to_say(*unsayable, answer):
    """The seeded renderer, except that each key in `unsayable` answers `answer(key)`."""
    seeded = _seeded("en")

    def render(key, /, **values):
        return answer(key) if key in unsayable else seeded(key, **values)

    return render


@pytest.mark.parametrize("attribution", [ETA_ATTRIBUTION_GOOGLE, None])
def test_nothing_promised_says_nothing(attribution):
    assert _lines(None, attribution=attribution) == []
    assert promised_time_lines(_seeded("en"), minutes_left=None, by_time=None, attribution=None) == []


def test_once_the_promised_time_has_passed_it_says_any_minute():
    assert _lines(0) == ["Arriving any minute", "Live traffic: Google Maps"]


@pytest.mark.parametrize(
    "minutes_left, sentence",
    [
        (1, "Arriving within about 1 min (by 14:29)"),
        (29, "Arriving within about 29 min (by 14:29)"),
    ],
)
def test_minutes_left_say_the_minutes_and_the_by_time(minutes_left, sentence):
    assert _lines(minutes_left) == [sentence, "Live traffic: Google Maps"]


@pytest.mark.parametrize(
    "language, minutes_left, lines",
    [
        ("uz", 29, ["Taxminan 29 daqiqa ichida yetib keladi (14:29 gacha)", "Jonli tirbandlik: Google Maps"]),
        ("uz", 0, ["Bir necha daqiqada yetib keladi", "Jonli tirbandlik: Google Maps"]),
        ("ru", 29, ["Прибудет в течение примерно 29 мин (до 14:29)", "Пробки: Google Maps"]),
        ("ru", 0, ["Прибудет с минуты на минуту", "Пробки: Google Maps"]),
    ],
)
def test_uzbek_and_russian_say_the_same(language, minutes_left, lines):
    assert _lines(minutes_left, language=language) == lines


def test_minutes_not_from_google_carry_no_attribution():
    assert _lines(29, attribution=None) == ["Arriving within about 29 min (by 14:29)"]
    assert _lines(0, attribution=None) == ["Arriving any minute"]


@pytest.mark.parametrize("by_time", [None, ""], ids=["no-by-time", "empty-by-time"])
@pytest.mark.parametrize("attribution", [ETA_ATTRIBUTION_GOOGLE, None])
@pytest.mark.parametrize("language", ["en", "uz", "ru"])
@pytest.mark.parametrize("minutes_left", [1, 29])
def test_minutes_with_no_by_time_say_nothing(minutes_left, language, attribution, by_time):
    """The sentence prints the time it is due by. Filled with a missing one it would read "(by None)", "(None gacha)"
    or "(до None)", so minutes that come with no time cannot be said, and neither can the attribution of a time."""
    assert _lines(minutes_left, language=language, attribution=attribution, by_time=by_time) == []


@pytest.mark.parametrize("by_time", [None, ""], ids=["no-by-time", "empty-by-time"])
def test_any_minute_needs_no_by_time(by_time):
    """A passed time prints no "by" time, so "Arriving any minute" is said without one."""
    assert _lines(0, by_time=by_time) == ["Arriving any minute", "Live traffic: Google Maps"]


@pytest.mark.parametrize("answer", UNSAYABLE_ANSWERS)
@pytest.mark.parametrize(
    "minutes_left, attribution, unsayable",
    [
        pytest.param(29, ETA_ATTRIBUTION_GOOGLE, ON_THE_WAY, id="the-sentence"),
        pytest.param(29, ETA_ATTRIBUTION_GOOGLE, ATTRIBUTION, id="the-attribution-of-a-time"),
        pytest.param(0, ETA_ATTRIBUTION_GOOGLE, ARRIVING_NOW, id="any-minute"),
        pytest.param(0, ETA_ATTRIBUTION_GOOGLE, ATTRIBUTION, id="the-attribution-of-any-minute"),
        pytest.param(29, None, ON_THE_WAY, id="the-sentence-without-attribution"),
    ],
)
def test_a_line_the_renderer_cannot_say_silences_them_all(minutes_left, attribution, unsayable, answer):
    """All or nothing: never half a line, never a humanised key, and never a time from Google without its
    attribution."""
    render = _unable_to_say(unsayable, answer=answer)

    assert _lines(minutes_left, attribution=attribution, render=render) == []


# --------------------------------------------------------------------------- #
# track_lines: the customer bot's Track screen
# --------------------------------------------------------------------------- #


def _track(stage, *, minutes_left=None, attribution=None, language="en", render=None, **published):
    """`track_lines` for a Track body. `by_time` comes with the minutes, as `customer_eta` publishes them, unless a
    test publishes another."""
    published = {"by_time": BY if minutes_left is not None else None, **published}
    return track_lines(
        render or _seeded(language), stage=stage, minutes_left=minutes_left, attribution=attribution, **published
    )


@pytest.mark.parametrize(
    "minutes_left, sentence",
    [
        (1, "Arriving within about 1 min (by 14:29)"),
        (29, "Arriving within about 29 min (by 14:29)"),
    ],
)
def test_on_the_way_with_minutes_says_the_promised_time(minutes_left, sentence):
    assert _track(ETA_STAGE_ON_THE_WAY, minutes_left=minutes_left, attribution=ETA_ATTRIBUTION_GOOGLE) == [
        sentence,
        "Live traffic: Google Maps",
    ]
    # Minutes that did not come from Google carry no attribution.
    assert _track(ETA_STAGE_ON_THE_WAY, minutes_left=minutes_left) == [sentence]


def test_on_the_way_at_zero_minutes_says_any_minute():
    assert _track(ETA_STAGE_ON_THE_WAY, minutes_left=0, attribution=ETA_ATTRIBUTION_GOOGLE) == [
        "Arriving any minute",
        "Live traffic: Google Maps",
    ]
    assert _track(ETA_STAGE_ON_THE_WAY, minutes_left=0) == ["Arriving any minute"]


@pytest.mark.parametrize("attribution", [None, ETA_ATTRIBUTION_GOOGLE])
def test_on_the_way_with_no_minutes_says_on_the_way(attribution):
    """Review Focus #2: a driver whose position was stale at the tap gets no minutes. They are on the way, and that is
    all that is promised. Google's attribution goes with a time; with no time it is not said."""
    assert _track(ETA_STAGE_ON_THE_WAY, attribution=attribution) == ["On the way"]


ON_THE_WAY_SAYS = {"en": "On the way", "uz": "Yo'lda", "ru": "В пути"}


@pytest.mark.parametrize("by_time", [None, ""], ids=["no-by-time", "empty-by-time"])
@pytest.mark.parametrize("attribution", [ETA_ATTRIBUTION_GOOGLE, None])
@pytest.mark.parametrize("language", sorted(ON_THE_WAY_SAYS))
@pytest.mark.parametrize("minutes_left", [1, 29])
def test_on_the_way_with_minutes_but_no_by_time_says_on_the_way(minutes_left, language, attribution, by_time):
    """Minutes with no time they are due by cannot be said (see `test_minutes_with_no_by_time_say_nothing`), so Track
    says the driver is on the way: no "None" in any language, and no attribution line for a time that is not there."""
    lines = _track(
        ETA_STAGE_ON_THE_WAY, minutes_left=minutes_left, attribution=attribution, language=language, by_time=by_time
    )

    assert lines == [ON_THE_WAY_SAYS[language]]


def test_on_the_way_at_zero_minutes_needs_no_by_time():
    assert _track(ETA_STAGE_ON_THE_WAY, minutes_left=0, attribution=ETA_ATTRIBUTION_GOOGLE, by_time=None) == [
        "Arriving any minute",
        "Live traffic: Google Maps",
    ]


@pytest.mark.parametrize("attribution", [None, ETA_ATTRIBUTION_GOOGLE])
@pytest.mark.parametrize("minutes_left", [None, 0, 1, 29])
def test_arrived_says_arrived_whatever_else_is_published(minutes_left, attribution):
    assert _track(ETA_STAGE_ARRIVED, minutes_left=minutes_left, attribution=attribution) == ["Arrived at your address"]


@pytest.mark.parametrize("stage", [None, "", "delivered", "unknown_stage"])
def test_any_other_stage_says_nothing(stage):
    """Not even the minutes a payload still carries: the stage decides what is said."""
    assert _track(stage, minutes_left=29, attribution=ETA_ATTRIBUTION_GOOGLE) == []


@pytest.mark.parametrize(
    "language, stage, minutes_left, lines",
    [
        ("uz", ETA_STAGE_ON_THE_WAY, 29, ["Taxminan 29 daqiqa ichida yetib keladi (14:29 gacha)"]),
        ("uz", ETA_STAGE_ON_THE_WAY, None, ["Yo'lda"]),
        ("uz", ETA_STAGE_ARRIVED, None, ["Manzilingizga yetib keldi"]),
        ("ru", ETA_STAGE_ON_THE_WAY, 29, ["Прибудет в течение примерно 29 мин (до 14:29)"]),
        ("ru", ETA_STAGE_ON_THE_WAY, None, ["В пути"]),
        ("ru", ETA_STAGE_ARRIVED, None, ["Доставка прибыла по вашему адресу"]),
    ],
)
def test_uzbek_and_russian_say_the_same_on_the_track_screen(language, stage, minutes_left, lines):
    assert _track(stage, minutes_left=minutes_left, language=language) == lines


@pytest.mark.parametrize("answer", UNSAYABLE_ANSWERS)
@pytest.mark.parametrize(
    "minutes_left, attribution, unsayable",
    [
        pytest.param(29, ETA_ATTRIBUTION_GOOGLE, ON_THE_WAY, id="the-sentence"),
        pytest.param(29, ETA_ATTRIBUTION_GOOGLE, ATTRIBUTION, id="the-attribution-of-a-time"),
        pytest.param(0, ETA_ATTRIBUTION_GOOGLE, ARRIVING_NOW, id="any-minute"),
        pytest.param(29, None, ON_THE_WAY, id="the-sentence-without-attribution"),
    ],
)
def test_a_promise_the_renderer_cannot_say_leaves_on_the_way(minutes_left, attribution, unsayable, answer):
    """The promised time is all or nothing (never half a line, never a time from Google without its attribution), but
    the driver is on the way all the same."""
    render = _unable_to_say(unsayable, answer=answer)

    assert _track(ETA_STAGE_ON_THE_WAY, minutes_left=minutes_left, attribution=attribution, render=render) == [
        "On the way"
    ]


@pytest.mark.parametrize("answer", UNSAYABLE_ANSWERS)
@pytest.mark.parametrize(
    "minutes_left, attribution, unsayable",
    [
        pytest.param(None, None, (NO_TIME,), id="no-minutes"),
        pytest.param(29, ETA_ATTRIBUTION_GOOGLE, (ON_THE_WAY, NO_TIME), id="the-promise-too"),
    ],
)
def test_on_the_way_says_nothing_when_even_that_line_cannot_be_said(minutes_left, attribution, unsayable, answer):
    render = _unable_to_say(*unsayable, answer=answer)

    assert _track(ETA_STAGE_ON_THE_WAY, minutes_left=minutes_left, attribution=attribution, render=render) == []


@pytest.mark.parametrize("answer", UNSAYABLE_ANSWERS)
def test_arrived_says_nothing_when_its_line_cannot_be_said(answer):
    render = _unable_to_say(ARRIVED, answer=answer)

    assert _track(ETA_STAGE_ARRIVED, render=render) == []


def test_a_line_of_another_branch_does_not_matter():
    """Each stage needs only its own lines."""
    arrived_only = _unable_to_say(ON_THE_WAY, ARRIVING_NOW, ATTRIBUTION, NO_TIME, answer=lambda key: "")
    on_the_way_only = _unable_to_say(ARRIVED, ON_THE_WAY, ARRIVING_NOW, ATTRIBUTION, answer=humanise_key)

    assert _track(ETA_STAGE_ARRIVED, render=arrived_only) == ["Arrived at your address"]
    assert _track(ETA_STAGE_ON_THE_WAY, render=on_the_way_only) == ["On the way"]
