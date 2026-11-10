"""The operator dashboard (#571, docs/23 §5): an app of its own, run from the
API's image with its own database and migrations (`db/dashboard/`).

It never imports `api.config` or `api.db`: those build the customer stack's
settings and connection, which the operator stack does not have.
"""
