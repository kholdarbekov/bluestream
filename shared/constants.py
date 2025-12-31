"""
Shared constants for the Water Business Platform.
These constants are used by both business_app and telegram_bot.
"""

# Geographic Constants
TASHKENT_COORDINATES = {
    'latitude': 41.2995,
    'longitude': 69.2401
}

# Tashkent City Boundary (approximate bounding box for validation)
# Used to validate that delivery addresses are within the service area
TASHKENT_BOUNDS = {
    'min_lat': 41.15,
    'max_lat': 41.45,
    'min_lng': 69.05,
    'max_lng': 69.45
}


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


def is_within_tashkent(latitude: float, longitude: float) -> bool:
    """Check if coordinates are within Tashkent city bounds"""
    return (TASHKENT_BOUNDS['min_lat'] <= latitude <= TASHKENT_BOUNDS['max_lat'] and
            TASHKENT_BOUNDS['min_lng'] <= longitude <= TASHKENT_BOUNDS['max_lng'])


def get_geo_config(language: str = 'en') -> dict:
    """Get all geographic configuration for frontend/bot use"""
    return {
        'center': TASHKENT_COORDINATES,
        'bounds': TASHKENT_BOUNDS,
        'districts': get_all_districts(language)
    }
