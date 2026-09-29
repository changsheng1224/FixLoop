from parser import extract


def run(value: str) -> tuple[str, str]:
    return extract(value), extract(value)
