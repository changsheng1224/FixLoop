def validate_age(age):
    if age < 0:
        raise ValueError("invalid age")
    return age
