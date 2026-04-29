"""Chronogolf adapter — placeholder.

Mangrove Bay's tee sheet shows up on Chronogolf, but bookings for it are NOT
actually taken there (the user confirmed this with the course). Chronogolf
support stays in scope for OTHER courses that genuinely back-end on it.

When a real Chronogolf-backed course is targeted, add `base.py` here mirroring
foreup/base.py: shared HTTP client + per-course subclass files. Endpoints,
auth shape, and CSRF posture must be confirmed in their own spike before
implementation begins (track as Spike S2).
"""
