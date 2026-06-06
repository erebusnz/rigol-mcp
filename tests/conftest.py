"""Shared pytest fixtures and VISA fakes.

All tests run fully offline — there is no real instrument. The pyvisa ``Resource`` and
``ResourceManager`` are replaced with small fakes that record interactions and replay
canned responses, so the SCPI/transport logic can be exercised deterministically.
"""

import pytest

from rigol_mcp import scope as scope_mod


def make_block(payload: bytes, ndigits: int = 9) -> bytes:
    """Build an IEEE 488.2 definite-length block: ``#N<length><payload>\\n``.

    Mirrors what a DS1000Z returns for :WAV:DATA? / :DISPlay:DATA? (9-digit length).
    """
    header = b"#" + str(ndigits).encode() + f"{len(payload):0{ndigits}d}".encode()
    return header + payload + b"\n"


class FakeScope:
    """Stand-in for a pyvisa message-based ``Resource``.

    - ``query`` replays from a ``responses`` dict (exact match, then prefix match).
    - ``read_bytes``/``read_raw`` consume a preloaded byte buffer (use ``load`` to set it).
    - ``write`` is recorded in ``written``.
    Attribute writes (timeout, chunk_size, terminations) are just stored, like the real one.
    """

    def __init__(self, responses=None, read_buffer: bytes = b"",
                 resource_name: str = "USB0::0xFAKE::INSTR"):
        self.responses = dict(responses or {})
        self._buf = bytes(read_buffer)
        self._pos = 0
        self.resource_name = resource_name
        self.timeout = None
        self.chunk_size = None
        self.read_termination = None
        self.write_termination = None
        self.written: list[str] = []
        self.cleared = 0
        self.closed = False

    # --- data the device will "send" on the next read(s) ---
    def load(self, data: bytes) -> None:
        self._buf = bytes(data)
        self._pos = 0

    # --- pyvisa Resource API surface used by the code under test ---
    def query(self, cmd: str) -> str:
        key = cmd.strip()
        if key in self.responses:
            val = self.responses[key]
            return val() if callable(val) else val
        for prefix, val in self.responses.items():
            if key.startswith(prefix):
                return val() if callable(val) else val
        raise KeyError(f"FakeScope: no canned response for query {cmd!r}")

    def write(self, cmd: str) -> None:
        self.written.append(cmd)

    def read_bytes(self, count: int) -> bytes:
        chunk = self._buf[self._pos:self._pos + count]
        self._pos += count
        return chunk

    def read_raw(self, size=None) -> bytes:
        chunk = self._buf[self._pos:]
        self._pos = len(self._buf)
        return chunk

    def clear(self) -> None:
        self.cleared += 1

    def close(self) -> None:
        self.closed = True


class FakeResourceManager:
    """Stand-in for ``pyvisa.ResourceManager``."""

    def __init__(self, resources=(), scope: FakeScope | None = None, scope_factory=None):
        self._resources = tuple(resources)
        self._scope = scope
        self._scope_factory = scope_factory
        self.closed = False

    def list_resources(self):
        return self._resources

    def open_resource(self, name, *args, **kwargs):
        if self._scope_factory is not None:
            return self._scope_factory(name)
        if self._scope is None:
            self._scope = FakeScope(resource_name=name)
        return self._scope

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def clean_env_and_cache(monkeypatch):
    """Isolate every test: clear RIGOL_* env (incl. anything loaded from .env) and reset
    the module-level cached connection / backend hint in rigol_mcp.scope."""
    for var in ("RIGOL_IP", "RIGOL_USB", "RIGOL_USB_SERIAL", "RIGOL_SCREENSHOT_DIR",
                "RIGOL_ENABLE_SEND_RAW"):
        monkeypatch.delenv(var, raising=False)
    scope_mod._rm = None
    scope_mod._scope = None
    scope_mod._usb_backend_hint = None
    yield
    scope_mod._rm = None
    scope_mod._scope = None
    scope_mod._usb_backend_hint = None
