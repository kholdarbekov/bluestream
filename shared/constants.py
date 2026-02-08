"""
Shared constants for the Water Business Platform.
These constants are used by both business_app and telegram_bot.
"""
import os

# ─── Timezone (Single Source of Truth) ─────────────────────────────────
# Override via DISPLAY_TIMEZONE / DISPLAY_TIMEZONE_OFFSET in .env
DISPLAY_TIMEZONE = os.environ.get('DISPLAY_TIMEZONE', 'Asia/Tashkent')
DISPLAY_TIMEZONE_OFFSET = os.environ.get('DISPLAY_TIMEZONE_OFFSET', '+05:00')

# ─── Status Icon Mappings ───────────────────────────────────────────────

ORDER_STATUS_ICONS = {
    'created': '📝',
    'pending': '🕐',
    'confirmed': '✅',
    'preparing': '👨\u200d🍳',
    'out_for_delivery': '🚚',
    'delivered': '📦',
    'cancelled': '❌',
    'returned': '↩️',
}

SUBSCRIPTION_STATUS_ICONS = {
    'active': '✅',
    'paused': '⏸️',
    'cancelled': '❌',
    'expired': '⏰',
    'trial': '🎁',
}

DEFAULT_STATUS_ICON = '📋'

# ─── Geographic Constants ───────────────────────────────────────────────
TASHKENT_COORDINATES = {
    'latitude': 41.2995,
    'longitude': 69.2401
}

# Tashkent City Boundary (approximate bounding box for validation)
# Used to validate that delivery addresses are within the service area
# Note: This is now a fallback for bounding box checks, primary validation
# uses TASHKENT_POLYGON
TASHKENT_BOUNDS = {
    'min_lat': 41.15,
    'max_lat': 41.45,
    'min_lng': 69.05,
    'max_lng': 69.45
}

# Tashkent City Polygon Boundary
# Define the precise delivery area polygon (lat, lng pairs)
# Currently set to a rough bounding box, but can be updated with precise shape
TASHKENT_POLYGON = [
    [41.32204059876415, 69.09754166856911],
    [41.275012216877286, 69.08710356336425],
    [41.23892917485904, 69.08292538164275],
    [41.20910564043868, 69.08083738499434],
    [41.17769335004607, 69.02657190847538],
    [41.158837254233276, 68.99108818803236],
    [41.12425177824721, 68.96394730812446],
    [41.09908670002474, 68.98690145258692],
    [41.06760919518189, 69.01611382886827],
    [41.03454457406531, 69.0474199736631],
    [41.022476814881685, 69.15935897969757],
    [41.01502880786373, 69.29622092205113],
    [41.00421841667105, 69.37569461857586],
    [41.02235378581986, 69.39195306419285],
    [41.04495668043211, 69.39401310480454],
    [41.08153702272071, 69.40662933973229],
    [41.115336489795425, 69.45346514311686],
    [41.158033948883286, 69.45953431611665],
    [41.20950432245618, 69.50951519740696],
    [41.29127880627493, 69.52494900833051],
    [41.334795712076556, 69.55895284658473],
    [41.389981391181635, 69.51027876824216],
    [41.427801153472075, 69.45285739550994],
    [41.4253074265265, 69.34803376817996],
    [41.42403449021862, 69.28656269219832],
    [41.43109153742682, 69.22854304294685],
    [41.40893577808734, 69.18905390552135],
    [41.391505906087076, 69.16815407886656],
    [41.36362587173181, 69.12227852247975],
    [41.360141982898796, 69.11879095356554],
    [41.32204059876415, 69.09754166856911],
]


# Tashkent City Districts with multilingual names and center coordinates
TASHKENT_DISTRICTS = {
    'almazar': {'en': 'Almazar', 'uz': 'Olmazor', 'ru': 'Алмазар', 'center': (41.3284, 69.2166)},
    'bektemir': {'en': 'Bektemir', 'uz': 'Bektemir', 'ru': 'Бектемир', 'center': (41.2104, 69.3341)},
    'chilanzar': {'en': 'Chilanzar', 'uz': 'Chilonzor', 'ru': 'Чиланзар', 'center': (41.2811, 69.1817)},
    'hamza': {'en': 'Hamza', 'uz': 'Hamza', 'ru': 'Хамза', 'center': (41.3399, 69.2779)},
    'mirobod': {'en': 'Mirobod', 'uz': 'Mirobod', 'ru': 'Мирабад', 'center': (41.2944, 69.2794)},
    'mirzo_ulugbek': {'en': 'Mirzo Ulugbek', 'uz': "Mirzo Ulug'bek", 'ru': 'Мирзо Улугбек', 'center': (41.3439, 69.3269)},
    'sergeli': {'en': 'Sergeli', 'uz': 'Sergeli', 'ru': 'Сергели', 'center': (41.2289, 69.2151)},
    'shaykhontohur': {'en': 'Shaykhontohur', 'uz': 'Shayxontohur', 'ru': 'Шайхантаур', 'center': (41.3189, 69.2442)},
    'uchtepa': {'en': 'Uchtepa', 'uz': 'Uchtepa', 'ru': 'Учтепа', 'center': (41.3042, 69.1503)},
    'yakkasaray': {'en': 'Yakkasaray', 'uz': 'Yakkasaroy', 'ru': 'Яккасарай', 'center': (41.2808, 69.2608)},
    'yashnobod': {'en': 'Yashnobod', 'uz': 'Yashnobod', 'ru': 'Яшнабад', 'center': (41.2925, 69.3297)},
    'yunusabad': {'en': 'Yunusabad', 'uz': 'Yunusobod', 'ru': 'Юнусабад', 'center': (41.3656, 69.2856)},
}


def get_district_name(district_key: str, language: str = 'en') -> str:
    """Get district display name in specified language"""
    district = TASHKENT_DISTRICTS.get(district_key)
    if district:
        return district.get(language, district.get('en', district_key))
    return district_key


def get_district_center(district_key: str) -> tuple:
    """Get district center coordinates for geocoding hints"""
    district = TASHKENT_DISTRICTS.get(district_key)
    if district:
        return district.get('center', (41.2995, 69.2401))
    return (41.2995, 69.2401)  # Default: Tashkent center


def get_all_districts(language: str = 'en') -> list:
    """Get all districts as a list with keys and display names"""
    return [
        {'key': key, 'name': data.get(language, data.get('en', key))}
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

        if ((yi < y <= yj) or (yj < y <= yi)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            odd_nodes = not odd_nodes
        j = i

    return odd_nodes


def is_within_tashkent(latitude: float, longitude: float) -> bool:
    """Check if coordinates are within Tashkent city bounds using polygon"""
    return point_in_polygon((latitude, longitude), TASHKENT_POLYGON)


def get_geo_config(language: str = 'en') -> dict:
    """Get all geographic configuration for frontend/bot use"""
    return {
        'center': TASHKENT_COORDINATES,
        'bounds': TASHKENT_BOUNDS,
        'polygon': TASHKENT_POLYGON,
        'districts': get_all_districts(language)
    }
