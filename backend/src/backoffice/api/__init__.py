"""HTTP layer. ``backoffice.api.app`` is the FastAPI wrapper over ``backoffice.service``.

Nothing is imported here, so ``backoffice.service`` stays importable without FastAPI
(the browser build runs it under Pyodide).
"""
