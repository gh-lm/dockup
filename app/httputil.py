"""Small helpers shared by the API modules."""

from flask import request


def json_body() -> dict:
    """The request body as a dict. A body that is not a JSON object (a
    list, a bare number, nothing at all) reads as empty, so the endpoint
    answers with its own "missing field" message rather than a 500."""
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}
