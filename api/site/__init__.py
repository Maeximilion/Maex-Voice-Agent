"""The public site of the product (#892): the landing page and the inquiry page.

A small app of its own, like the operator dashboard: no database, no session, no
secret, and nothing of the customer application is imported. Run it with
`uvicorn api.site.app:app --port 8003`.
"""
