"""Dev / local-run helpers. Importable from the CLI for `--use-fake-adapter`
mode and from tests as a scriptable orchestrator collaborator. NOT used in
production — wiring is gated by an explicit CLI flag.
"""

from .fake_adapter import FakeAdapter

__all__ = ["FakeAdapter"]
