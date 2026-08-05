"""
Shared constants for the Water Business Platform.
These constants are used by both business_app and telegram_bot.
"""

import os

# ─── Timezone (Single Source of Truth) ─────────────────────────────────
# Override via DISPLAY_TIMEZONE / DISPLAY_TIMEZONE_OFFSET in .env
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "Asia/Tashkent")
DISPLAY_TIMEZONE_OFFSET = os.environ.get("DISPLAY_TIMEZONE_OFFSET", "+05:00")

# ─── Status Icon Mappings ───────────────────────────────────────────────

ORDER_STATUS_ICONS = {
    "created": "📝",
    "pending": "🕐",
    "confirmed": "✅",
    "preparing": "👨\u200d🍳",
    "out_for_delivery": "🚚",
    "delivered": "📦",
    "cancelled": "❌",
    "returned": "↩️",
}

SUBSCRIPTION_STATUS_ICONS = {
    "active": "✅",
    "paused": "⏸️",
    "cancelled": "❌",
    "expired": "⏰",
    "trial": "🎁",
}

DEFAULT_STATUS_ICON = "📋"

# ─── Geographic Constants ───────────────────────────────────────────────
TASHKENT_COORDINATES = {"latitude": 41.2995, "longitude": 69.2401}

# Tashkent City Polygon Boundary — SINGLE SOURCE OF TRUTH for the delivery
# coverage zone. Every address entry point (backend, bots, admin UI, web
# wizard) validates coordinates against this polygon via is_within_tashkent().
# Define the precise delivery area polygon (lat, lng pairs)
TASHKENT_POLYGON = [
    [41.29572219157228, 69.06029803371433],
    [41.29431778348942, 69.04603475906137],
    [41.28466064139431, 69.02364803670824],
    [41.274052079235275, 69.02445987839869],
    [41.2683113123, 69.03758497574776],
    [41.26602318770793, 69.04575681983212],
    [41.260354471618, 69.04719153402891],
    [41.25508864024181, 69.044138389247],
    [41.24798653765683, 69.05161027957226],
    [41.2441345553209, 69.05142992793122],
    [41.241630287400426, 69.05520942068463],
    [41.243165220538316, 69.0611731259371],
    [41.25801122756954, 69.06246272030063],
    [41.260570862488066, 69.06376326309916],
    [41.25994633166749, 69.07429004808418],
    [41.25786454329702, 69.0881132395676],
    [41.250028684701704, 69.0931710944406],
    [41.235039091913336, 69.11058422064374],
    [41.228366595836775, 69.11607138884548],
    [41.223917167265, 69.117619118215],
    [41.2101883, 69.1138855],
    [41.2060634261278, 69.1219623890629],
    [41.196100311061656, 69.12393513017159],
    [41.183038603160185, 69.13408618003979],
    [41.17824743780443, 69.14988963548404],
    [41.1643093, 69.1424794],
    [41.1617989, 69.1564236],
    [41.1591744, 69.1576361],
    [41.156263482157925, 69.1593367326837],
    [41.14392996876069, 69.15594639872955],
    [41.13638659080266, 69.16822652135218],
    [41.14776629566944, 69.1934804722114],
    [41.1348206, 69.2116328],
    [41.1241617, 69.2092167],
    [41.1079099, 69.1967914],
    [41.0928247, 69.2031766],
    [41.0755245, 69.2012783],
    [41.0666775, 69.190061],
    [41.0614728, 69.1835032],
    [41.0579594, 69.1767728],
    [41.0528728, 69.1350763],
    [41.0510101, 69.1271063],
    [41.0451904, 69.1241382],
    [41.0418575, 69.1240063],
    [41.0386239, 69.1245999],
    [41.0356505, 69.1242676],
    [41.0317698, 69.1255207],
    [41.0322673, 69.134095],
    [41.0393817, 69.1389758],
    [41.0441573, 69.1418119],
    [41.0469429, 69.1439225],
    [41.0464957, 69.1656219],
    [41.0649746, 69.2025528],
    [41.0727811, 69.2085929],
    [41.0811091, 69.2127345],
    [41.0981478, 69.2120442],
    [41.1128417, 69.2123894],
    [41.1232863, 69.218947],
    [41.125137870372384, 69.23114312953047],
    [41.12014735556002, 69.24989736538691],
    [41.122918639181364, 69.27596745610782],
    [41.127717993739964, 69.31133875173813],
    [41.13515358049355, 69.33046957841935],
    [41.15104860010018, 69.36343694307868],
    [41.16431251224486, 69.37862867101228],
    [41.183080506893845, 69.39028794980032],
    [41.20225389238081, 69.40165925655847],
    [41.213685808947986, 69.42146333778246],
    [41.23743174144093, 69.44668968824101],
    [41.25897504052227, 69.45399416836793],
    [41.28047553694444, 69.47146307336274],
    [41.301050508201996, 69.4849666793327],
    [41.3212419396354, 69.49388929398432],
    [41.32913322706884, 69.49740851806467],
    [41.33728823394267, 69.485016136241],
    [41.342692160816995, 69.46994439289256],
    [41.349836902503455, 69.46114737724457],
    [41.358380989349314, 69.45073798755396],
    [41.37576710514995, 69.472356921412],
    [41.38254564439151, 69.4836494834482],
    [41.3919519311238, 69.48841246671384],
    [41.400482851542606, 69.47431396553056],
    [41.40833067646134, 69.47099228009776],
    [41.41194289389843, 69.45815012141543],
    [41.40605473668344, 69.43924998425307],
    [41.401339370512915, 69.42621251304126],
    [41.39901157108244, 69.4174145697796],
    [41.39670915070491, 69.39803511995382],
    [41.40306469012285, 69.37673302978465],
    [41.406826328140625, 69.36590069765768],
    [41.40842497918052, 69.35344354311783],
    [41.40840847109686, 69.33915105373845],
    [41.40890952604127, 69.32936687024937],
    [41.412979128837065, 69.32328244111736],
    [41.4136866909852, 69.31136724013788],
    [41.41309598265437, 69.30302454837744],
    [41.41259107192164, 69.29549449046598],
    [41.4231457, 69.2932083],
    [41.4210364, 69.2855968],
    [41.4202919, 69.2803019],
    [41.4191752, 69.275007],
    [41.4240142, 69.2723596],
    [41.4219049, 69.2584604],
    [41.4256271, 69.2493598],
    [41.4282325, 69.23877],
    [41.4180585, 69.2318204],
    [41.41558746919634, 69.22532148309406],
    [41.4171899, 69.2162667],
    [41.4183066, 69.2104754],
    [41.4183066, 69.2045186],
    [41.416880809803246, 69.19685449857073],
    [41.41132491789244, 69.18663600991067],
    [41.407250616617205, 69.18174931805274],
    [41.39989938578927, 69.1806659038217],
    [41.395724469152555, 69.17813942487953],
    [41.391252179795515, 69.17509582356095],
    [41.39085717463129, 69.16848657467372],
    [41.382506281719685, 69.15669387623777],
    [41.37962440374602, 69.14862186619104],
    [41.385587629063195, 69.1405304943734],
    [41.38529044708821, 69.13177975447053],
    [41.37952478047623, 69.12090423268242],
    [41.37236661665281, 69.1113554040472],
    [41.36550013236243, 69.10258906382859],
    [41.36092199887952, 69.09887553404971],
    [41.35414640570278, 69.09474128906942],
    [41.347871384884456, 69.1012444203273],
    [41.34398952366561, 69.10708695604598],
    [41.3394091109256, 69.10390083699681],
    [41.33343366759928, 69.10190941660156],
    [41.326860561337526, 69.10137848165289],
    [41.32596619286906, 69.1050936815422],
    [41.31558086185228, 69.09657767899535],
    [41.30763142661857, 69.08957095951448],
    [41.299079956472184, 69.07924871665153],
    [41.2937059924522, 69.07064252617604],
    [41.29572219157228, 69.06029803371433]
]

# Tashkent City Districts with multilingual names and center coordinates
TASHKENT_DISTRICTS = {
    "almazar": {
        "en": "Almazar",
        "uz": "Olmazor",
        "ru": "Алмазар",
        "center": (41.3284, 69.2166),
    },
    "bektemir": {
        "en": "Bektemir",
        "uz": "Bektemir",
        "ru": "Бектемир",
        "center": (41.2104, 69.3341),
    },
    "chilanzar": {
        "en": "Chilanzar",
        "uz": "Chilonzor",
        "ru": "Чиланзар",
        "center": (41.2811, 69.1817),
    },
    "hamza": {
        "en": "Hamza",
        "uz": "Hamza",
        "ru": "Хамза",
        "center": (41.3399, 69.2779),
    },
    "mirobod": {
        "en": "Mirobod",
        "uz": "Mirobod",
        "ru": "Мирабад",
        "center": (41.2944, 69.2794),
    },
    "mirzo_ulugbek": {
        "en": "Mirzo Ulugbek",
        "uz": "Mirzo Ulug'bek",
        "ru": "Мирзо Улугбек",
        "center": (41.3439, 69.3269),
    },
    "sergeli": {
        "en": "Sergeli",
        "uz": "Sergeli",
        "ru": "Сергели",
        "center": (41.2289, 69.2151),
    },
    "shaykhontohur": {
        "en": "Shaykhontohur",
        "uz": "Shayxontohur",
        "ru": "Шайхантаур",
        "center": (41.3189, 69.2442),
    },
    "uchtepa": {
        "en": "Uchtepa",
        "uz": "Uchtepa",
        "ru": "Учтепа",
        "center": (41.3042, 69.1503),
    },
    "yakkasaray": {
        "en": "Yakkasaray",
        "uz": "Yakkasaroy",
        "ru": "Яккасарай",
        "center": (41.2808, 69.2608),
    },
    "yashnobod": {
        "en": "Yashnobod",
        "uz": "Yashnobod",
        "ru": "Яшнабад",
        "center": (41.2925, 69.3297),
    },
    "yunusabad": {
        "en": "Yunusabad",
        "uz": "Yunusobod",
        "ru": "Юнусабад",
        "center": (41.3656, 69.2856),
    },
}


def get_district_name(district_key: str, language: str = "en") -> str:
    """Get district display name in specified language"""
    district = TASHKENT_DISTRICTS.get(district_key)
    if district:
        return district.get(language, district.get("en", district_key))
    return district_key


def get_district_center(district_key: str) -> tuple:
    """Get district center coordinates for geocoding hints"""
    district = TASHKENT_DISTRICTS.get(district_key)
    if district:
        return district.get("center", (41.2995, 69.2401))
    return (41.2995, 69.2401)  # Default: Tashkent center


def get_all_districts(language: str = "en") -> list:
    """Get all districts as a list with keys and display names"""
    return [
        {"key": key, "name": data.get(language, data.get("en", key))}
        for key, data in TASHKENT_DISTRICTS.items()
    ]


def point_in_polygon(point: tuple, polygon: list) -> bool:
    """
    Check if a point (lat, lng) is inside a polygon using Ray Casting algorithm.
    :param point: tuple (lat, lng)
    :param polygon: list of [lat, lng] lists
    :return: bool
    """
    x, y = point
    odd_nodes = False
    j = len(polygon) - 1

    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]

        if ((yi < y <= yj) or (yj < y <= yi)) and (
            x < (xj - xi) * (y - yi) / (yj - yi) + xi
        ):
            odd_nodes = not odd_nodes
        j = i

    return odd_nodes


def is_within_tashkent(latitude: float, longitude: float) -> bool:
    """Check if coordinates are within Tashkent city bounds using polygon"""
    return point_in_polygon((latitude, longitude), TASHKENT_POLYGON)


def get_geo_config(language: str = "en") -> dict:
    """Get all geographic configuration for frontend/bot use.

    ``polygon`` is the single source of truth for the delivery coverage area;
    ``center`` is only a map-centering hint.
    """
    return {
        "center": TASHKENT_COORDINATES,
        "polygon": TASHKENT_POLYGON,
        "districts": get_all_districts(language),
    }


# Short, multilingual coverage copy — SSOT for non-DB callers (the public
# check-delivery endpoint, the product feed, llms.txt). The /coverage page may
# use richer DB-backed marketing copy on top of this.
DELIVERY_COVERAGE_SUMMARY = {
    "en": "All of Tashkent city plus neighbouring areas of the Tashkent Region.",
    "uz": "Butun Toshkent shahri va Toshkent viloyatining qo'shni hududlari.",
    "ru": "Весь город Ташкент и прилегающие районы Ташкентской области.",
}
DELIVERY_COVERAGE_REGION_NOTE = {
    "en": "neighbouring areas of the Tashkent Region",
    "uz": "Toshkent viloyatining qo'shni hududlari",
    "ru": "прилегающие районы Ташкентской области",
}


def get_delivery_coverage(language: str = "en") -> dict:
    """Single source of truth for the *published* delivery coverage zone.

    Reuses TASHKENT_POLYGON (the enforced boundary) and TASHKENT_DISTRICTS, and
    adds short multilingual summary/region-note copy so non-DB callers stay
    accurate. Flask-free and bot-importable.
    """
    lang = language if language in ("en", "uz", "ru") else "en"
    return {
        "city": "Tashkent",
        "summary": DELIVERY_COVERAGE_SUMMARY[lang],
        "region_note": DELIVERY_COVERAGE_REGION_NOTE[lang],
        # districts carry their center here (scoped to the coverage map's pins);
        # the shared get_all_districts() stays {key, name} for the public APIs.
        "districts": [
            {"key": key, "name": data.get(lang, data.get("en", key)), "center": list(data["center"])}
            for key, data in TASHKENT_DISTRICTS.items()
        ],
        "center": TASHKENT_COORDINATES,
        "polygon": TASHKENT_POLYGON,
    }


def get_geoshape_polygon() -> str:
    """TASHKENT_POLYGON as a schema.org GeoShape ``polygon`` value.

    schema.org GeoShape.polygon is a single string of space-separated
    ``lat,lng`` pairs.
    """
    return " ".join(f"{lat},{lng}" for lat, lng in TASHKENT_POLYGON)
