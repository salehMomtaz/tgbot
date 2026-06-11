# utils/id_validator.py

def is_valid_telegram_id(id_str: str) -> bool:
    """
    Validates if an input string is a logically valid Telegram User ID.
    Enforces numeric constraints and safe boundaries (Telegram IDs are generally between 5 and 11 digits).
    """
    clean_str = id_str.strip()
    if not clean_str.isdigit():
        return False
        
    target_id = int(clean_str)
    # Check boundaries to prevent out-of-bounds integer overflow (min: 10000, max: 99999999999)
    if not (10000 <= target_id <= 99999999999):
        return False
        
    return True
