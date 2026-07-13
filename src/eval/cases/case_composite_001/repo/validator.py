def validate_name(name):
    return name.strip()  # BUG: None.strip() → AttributeError
