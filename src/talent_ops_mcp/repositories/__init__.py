"""Data access, behind protocols.

:mod:`~talent_ops_mcp.repositories.protocols` states what the services above may ask for;
:mod:`~talent_ops_mcp.repositories.sqlite` is the only implementation today. Phases 9-10 add
ATS-backed readers, and the seam is here so that work does not reach up into service code.
"""
