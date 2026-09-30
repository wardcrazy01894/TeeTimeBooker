"""Multi-user web app (MULTIUSER_PLAN §8): FastAPI + Jinja2 templates + invite-only OAuth
(authlib), served by ``teetime web`` as the ``teetime-web-<env>`` Container App. Layering:
``web`` imports ``tenant`` + ``core`` + ``courses``; nothing imports ``web``.
"""
