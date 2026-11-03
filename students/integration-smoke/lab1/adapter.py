"""Integration smoke test. Intentionally wrong adapter API version."""
API_VERSION = 99

def create_model(**options):
    raise NotImplementedError("Integration smoke test")
