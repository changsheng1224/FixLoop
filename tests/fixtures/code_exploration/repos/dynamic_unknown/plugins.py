def dispatch(plugin: object, name: str, value: str) -> object:
    handler = getattr(plugin, name)
    return handler(value)
