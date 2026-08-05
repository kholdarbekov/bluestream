# Re-export unified TimestampMixin from base module (timezone-aware)
from business_app.models.base import TimestampMixin  # noqa: F401

# Import all model modules so every mapped class is registered with the
# SQLAlchemy registry before the first query runs. Without this, string-based
# relationships (e.g. User.cart -> "Cart") can fail to resolve at mapper
# configuration time when the referenced module hasn't been imported yet by
# any code path in the current worker process.
from business_app.models import translatable  # noqa: F401, E402
from business_app.models import translation  # noqa: F401, E402
from business_app.models import product  # noqa: F401, E402
from business_app.models import user  # noqa: F401, E402
from business_app.models import cart  # noqa: F401, E402
from business_app.models import corporate  # noqa: F401, E402
from business_app.models import order  # noqa: F401, E402
from business_app.models import order_sequence  # noqa: F401, E402
from business_app.models import delivery  # noqa: F401, E402
from business_app.models import payment  # noqa: F401, E402
from business_app.models import subscription  # noqa: F401, E402
from business_app.models import review  # noqa: F401, E402
from business_app.models import loyalty  # noqa: F401, E402
from business_app.models import notification  # noqa: F401, E402
from business_app.models import analytics  # noqa: F401, E402
from business_app.models import audit  # noqa: F401, E402
from business_app.models import blog  # noqa: F401, E402
from business_app.models import bottle  # noqa: F401, E402
from business_app.models import staff  # noqa: F401, E402
from business_app.models import tryout  # noqa: F401, E402
from business_app.models import marking_code_config  # noqa: F401, E402
from business_app.models import marking_code_task_run  # noqa: F401, E402
from business_app.models import support  # noqa: F401, E402
from business_app.models.customer_link import (  # noqa: F401, E402
    AddressGroup,
    CanonicalCustomer,
    CustomerDistinctPair,
    CustomerLinkEvent,
    PlaceSuggestionDismissal,
)
