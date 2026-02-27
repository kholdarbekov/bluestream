"""Search-related helper functions for staff bot handlers."""


def detect_search_type(query_text: str) -> str:
    """Infer whether query should use phone or name search."""
    compact = (
        query_text
        .replace('+', '')
        .replace(' ', '')
        .replace('-', '')
        .replace('(', '')
        .replace(')', '')
    )
    return 'phone' if compact.isdigit() else 'name'
