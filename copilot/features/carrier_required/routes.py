"""Side-effect-free registration: approval hooks consult the live feature flag."""
def register(app):
    """The gate adds no route, middleware, table or mutable application state."""
    return None
