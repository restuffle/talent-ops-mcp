"""MCP tool adapters (§6.1).

An adapter declares the tool's name, annotations, description, and typed parameters, opens a
session, and hands the work to a service. It contains no recruiting rules -- if a decision about
the domain is being made here, it is in the wrong layer.

``talent_ops_health_check`` stays in :mod:`talent_ops_mcp.server`. It reports on the server
itself rather than on recruiting data, it needs no session, and relocating a working, tested
tool would be churn with nothing behind it.
"""
