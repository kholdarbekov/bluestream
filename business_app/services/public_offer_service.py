"""The public offer facts: what Aqua Element sells, at what price, in what smallest order and with what delivery fee
(plan 2026-11-12, "live price and same-day delivery visibility").

Every public surface (the homepage, /shop, the product and blog pages, the FAQ, `llms.txt`, the public products feed)
prints these facts and derives none of its own. The unit price is the one an order is charged
(`Product.catalogue_unit_price`); the smallest order clears both the product's own minimum and the order floor; the
delivery line follows the configured fee. Amounts stay `Decimal` inside the facts, and a number that leaves through
JSON goes through `_json_number`. Money and litre text come from `display_labels` only.

The size a surface prints is the product's volume (`Product.volume`) and nothing else: the trade size (`Product.size`,
5L/10L/19L) is never printed (owner, 2026-11-15). A product that records no volume prints no size: copy that names a
product by its size reads its `label` (the volume, else the product's name), the price line has a sizeless twin, and
the FAQ sizes answer is left out. The trade size only lets a shop search for "19 л" find the 18.9 l bottle
(`size_search_terms`).

Plain functions over the models and the config, both read on every call. Nothing is memoised: in tests one app context
outlives many requests, so a memo on `g` would serve the first page's facts to every later one. The module never
imports `business_app.frontend` and never touches `request`, `url_for` or `g`, so it runs outside a request too.
"""

from decimal import Decimal
from math import ceil, floor
from urllib.parse import quote

from flask import current_app

from business_app.models.product import Product
from business_app.utils import display_labels
from business_app.utils.local_windows import local_date
from business_app.utils.translations import get_translation
from shared.constants import DELIVERY_STATEMENT_REVIEWED_ON, get_delivery_statement

LANGUAGES = ("uz", "ru", "en")
CURRENCY = display_labels.CURRENCY
# The two sentences behind `facts["delivery"]["line"]`; the second fills `{fee}` with the fee's money text.
FREE_DELIVERY_KEY = "landing.offer.free_delivery"
DELIVERY_FEE_KEY = "landing.offer.delivery_fee"
EVERY_DAY_KEY = "landing.offer.every_day"
# The one sentence that names a product's price; a product page with no written description describes itself with it.
PRICE_LINE_KEY = "landing.offer.price_line"
SMALLEST_ORDER_KEY = "landing.offer.smallest_order"
# The price line of a product that records no volume: it drops the size ("<name>, <size>" would name it twice).
PRICE_LINE_NO_SIZE_KEY = "landing.offer.price_line_no_size"
HOME_META_KEY = "landing.offer.meta_home"
FAQ_KEY = "landing.offer.faq."
NO_DEPOSIT_KEY = "landing.offer.no_deposit"
_SCHEMA_ORG = "https://schema.org/"
# The characters Werkzeug leaves unescaped in a path segment: a slug quoted with them equals `url_for` for any
# routable slug. Shared by the product path here and the blog path in `public_urls_service`.
SLUG_SAFE = "!$&'()*+,:;=@"


def _json_number(value) -> int | float:
    """`value` as a JSON number: an int when it is whole, else a float. A `Decimal` is not JSON (`json.dumps` raises
    and Flask writes it as a string)."""
    amount = Decimal(str(value))
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _config_amount(name: str) -> Decimal:
    return Decimal(str(current_app.config[name]))


def _money_text(amount: Decimal, languages) -> dict:
    return {language: display_labels.money(amount, language) for language in languages}


def product_public_path(product) -> str:
    """The product's public page, as the routes serve it: `/product/<slug>` when it has a slug (percent-encoded as
    `url_for` writes it), else `/product/<id>`. The one builder of this path in Python."""
    return f"/product/{quote(product.slug, safe=SLUG_SAFE)}" if product.slug else f"/product/{product.id}"


def _smallest_order_quantity(product, unit_price: Decimal) -> int:
    """The fewest units of `product` an order may hold: its own minimum, raised until the items clear the order floor.
    Items only, the cart's floor check (`CartService.calculate_cart_estimate`), which also clears the order's check
    on items plus fee (`OrderService.create_order`) whatever the fee."""
    minimum = max(1, product.min_order_quantity or 1)
    if unit_price <= 0:
        return minimum
    return max(minimum, ceil(_config_amount("MIN_ORDER_AMOUNT") / unit_price))


def _size_text(litres: Decimal | None, languages) -> dict | None:
    """The size as printed, per language: the volume alone ("18,9 l" / "18,9 л" / "18.9 L"). None when the product
    records no volume: it then prints no size at all, never the trade size."""
    if litres is None:
        return None
    return {language: display_labels.litres(litres, language) for language in languages}


def size_search_terms(product_facts: dict, languages=LANGUAGES) -> list[str]:
    """What a size search may match for one product: its printed size in every language, plus its trade size
    ("19 л" / "19 l" / "19 L" for the 18.9 l bottle), the number buyers type. Search only: no page prints the trade
    size."""
    size_text = product_facts["size_text"]
    terms = list(size_text.values()) if size_text else []
    size_code = product_facts["size_code"]
    if size_code:
        trade_litres = Decimal(size_code.removesuffix("L"))
        terms += [display_labels.litres(trade_litres, language) for language in languages]
    return terms


def product_offer_facts(product, languages=LANGUAGES) -> dict:
    """One product's offer facts. Callers pass active rows; the function itself does not filter."""
    unit_price = product.catalogue_unit_price
    base_price = Decimal(str(product.base_price or 0))
    # A list price only when the charged price is below it: a discount at zero or above the base price is not one.
    list_price = base_price if base_price > unit_price else None
    litres = Decimal(str(product.volume)) if product.volume else None
    size_code = getattr(product.size, "value", product.size)
    quantity = _smallest_order_quantity(product, unit_price)
    smallest_order_total = unit_price * quantity
    returnable = product.is_returnable_bottle
    name = {language: product.get_translated("name", language) for language in languages}
    size_text = _size_text(litres, languages)
    return {
        "id": product.id,
        "sku": product.sku,
        "slug": product.slug,
        "path": product_public_path(product),
        "name": name,
        "litres": litres,
        # The trade size ("19L"): decisions and search only (the hero, the bottle group, `size_search_terms`), never
        # printed.
        "size_code": size_code,
        # The volume as printed, or None: a product without a volume prints no size.
        "size_text": size_text,
        # The product as copy names it by its size: its volume, or its name when it records no volume. The one place
        # that rule lives; the smallest order, the home meta description, the bottle group's decision rule and the
        # photo and link labels (`offer_sized_label`) all read it.
        "label": size_text or name,
        "unit_price": unit_price,
        "list_price": list_price,
        "price_text": _money_text(unit_price, languages),
        "list_price_text": _money_text(list_price, languages) if list_price is not None else None,
        "discount_percent": floor((list_price - unit_price) * 100 / list_price) if list_price is not None else None,
        "smallest_order_quantity": quantity,
        "smallest_order_total": smallest_order_total,
        "smallest_order_total_text": _money_text(smallest_order_total, languages),
        "returnable": returnable,
        # The returnable bottle is lent with no deposit: the courier swaps the empty at the next delivery.
        "no_deposit": returnable,
        # A marking-code product's stock is its code pool, which gates only code-consuming rails.
        "available": not product.stock_caps_purchase() or (product.stock_quantity or 0) > 0,
    }


def price_sentence(product_facts: dict, language: str) -> str:
    """The sentence "<name>, <size> — price <price>." for one product, in `language`; "<name> — price <price>." for a
    product with no size. Without its translation row it falls back to "<name> — <price>", so the price is named
    either way."""
    name = product_facts["name"][language]
    price = product_facts["price_text"][language]
    size_text = product_facts["size_text"]
    key, size = (PRICE_LINE_KEY, {"size": size_text[language]}) if size_text else (PRICE_LINE_NO_SIZE_KEY, {})
    sentence = get_translation(key, language, name=name, price=price, **size)
    return f"{name} — {price}" if sentence == key else sentence


def smallest_order_sentence(product_facts: dict, language: str) -> str:
    """The sentence "Smallest order: <qty> × <label> = <total>." for one product, in `language`: the label is its size,
    or its name when it records no volume."""
    return get_translation(
        SMALLEST_ORDER_KEY,
        language,
        qty=product_facts["smallest_order_quantity"],
        size=product_facts["label"][language],
        total=product_facts["smallest_order_total_text"][language],
    )


def home_meta_description(facts: dict, language: str) -> str | None:
    """The homepage meta description: every active product's "<label> — <price>" (the label is its size, or its name
    when it records no volume), in `language`. None when no product is listed, so the layout prints its default."""
    prices = ", ".join(
        f"{product['label'][language]} — {product['price_text'][language]}" for product in facts["products"]
    )
    return get_translation(HOME_META_KEY, language, prices=prices) if prices else None


def build_offer_faq(facts: dict, language: str) -> list[dict]:
    """The FAQ entries that state the offer facts, as `[{"id", "q", "a"}, ...]` of plain `str` (never `Markup`, so the
    FAQPage JSON-LD carries the same text the page shows): price, sizes and smallest order, then the deposit answer
    when a product is lent with no deposit. Empty when no product is listed.

    The sizes answer lists every listed product's size, so it is left out when any product records no volume: listing
    the others would say they are all the bottles there are."""
    products = facts["products"]
    if not products:
        return []
    answers = {
        "price": " ".join(
            [*(price_sentence(product, language) for product in products), facts["delivery"]["line"][language]]
        )
    }
    if all(product["size_text"] for product in products):
        sizes = ", ".join(product["size_text"][language] for product in products)
        answers["sizes"] = get_translation(f"{FAQ_KEY}sizes_a", language, sizes=sizes)
    answers["minimum"] = " ".join(smallest_order_sentence(product, language) for product in products)
    if any(product["no_deposit"] for product in products):
        answers["deposit"] = get_translation(NO_DEPOSIT_KEY, language)
    return [
        {"id": entry_id, "q": get_translation(f"{FAQ_KEY}{entry_id}_q", language), "a": answer}
        for entry_id, answer in answers.items()
    ]


def _delivery_facts(languages) -> dict:
    fee = _config_amount("DEFAULT_DELIVERY_FEE")
    free = fee <= 0
    fee_text = _money_text(fee, languages)
    line = {
        language: (
            get_translation(FREE_DELIVERY_KEY, language)
            if free
            else get_translation(DELIVERY_FEE_KEY, language, fee=fee_text[language])
        )
        for language in languages
    }
    return {
        "fee": fee,
        "free": free,
        "fee_text": fee_text,
        "line": line,
        "every_day": {language: get_translation(EVERY_DAY_KEY, language) for language in languages},
        # The approved sentences, printed whole by every surface and never re-worded.
        "statement": {language: get_delivery_statement(language) for language in languages},
        "statement_reviewed_on": DELIVERY_STATEMENT_REVIEWED_ON,
    }


def get_public_offer_facts(languages=LANGUAGES) -> dict:
    """The offer facts every public surface prints: today's date in Tashkent, the order floor, the delivery fee and
    its sentence, and the active products, dearest first."""
    products = Product.query.filter_by(is_active=True).order_by(Product.base_price.desc(), Product.id.asc()).all()
    return {
        "as_of": local_date().isoformat(),
        "currency": CURRENCY,
        "min_order_amount": _config_amount("MIN_ORDER_AMOUNT"),
        "delivery": _delivery_facts(languages),
        "products": [product_offer_facts(product, languages) for product in products],
    }


def build_offer_jsonld(product_facts: dict, *, url: str) -> dict:
    """The schema.org `Offer` for one product's facts, in plain JSON numbers.

    It carries no `shippingDetails`. Google's merchant-listing guide lists `deliveryTime` among the required
    properties of `OfferShippingDetails`, and no JSON-LD of this site states a delivery time, so shipping details
    would be ineligible; the fee reaches visitors in the delivery line (`facts["delivery"]["line"]`) instead.
    """
    return {
        "@type": "Offer",
        "priceCurrency": CURRENCY,
        "price": _json_number(product_facts["unit_price"]),
        "availability": f"{_SCHEMA_ORG}{'InStock' if product_facts['available'] else 'OutOfStock'}",
        "url": url,
        "eligibleQuantity": {"@type": "QuantitativeValue", "minValue": product_facts["smallest_order_quantity"]},
    }


def public_price_range() -> str | None:
    """The price range, "UZS 12340-23450", or "UZS 23450" when every price is the same: the unit prices above zero
    of the active products. One query and no copy, so every page can afford it. None when no active product has a
    price."""
    prices = [product.catalogue_unit_price for product in Product.query.filter_by(is_active=True).all()]
    prices = [price for price in prices if price > 0]
    if not prices:
        return None
    low, high = _json_number(min(prices)), _json_number(max(prices))
    return f"{CURRENCY} {low}" if low == high else f"{CURRENCY} {low}-{high}"
