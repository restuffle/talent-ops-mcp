"""Recruiting rules, scheduling, confirmation, and auditing (§6.1).

Services own the domain logic and the shape of what a tool returns. They depend on repository
protocols, never on the MCP layer, so the same rules can be exercised in a plain test without a
client session.
"""
