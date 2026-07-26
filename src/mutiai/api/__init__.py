"""Versioned HTTP API boundary.

Import the concrete Router from :mod:`mutiai.api.router`. Keeping package
initialization side-effect free prevents schema and error imports from loading
the complete application dependency graph.
"""
