"""Per-family SCPI dialect drivers for Rigol oscilloscopes.

Each supported scope family is a :class:`ScopeDriver` subclass encapsulating the SCPI
commands and conventions that differ between families. Generic connection and tool logic
lives in ``scope.py`` and delegates the family-specific parts to the active driver, which
is selected from ``*IDN?`` once per connection.

To add support for a new Rigol family:
  1. Subclass :class:`ScopeDriver` below and override the attributes/methods that differ.
  2. Implement :meth:`matches` so it recognises the family from its ``*IDN?`` string.
  3. Add an instance to ``_DRIVERS`` (list more specific families before broader ones).
Nothing in ``scope.py`` should need to change.

Drivers are stateless singletons: every method receives the live pyvisa ``scope`` rather
than holding a reference, so one instance per family is shared across all connections.
"""

import pyvisa

# DS1000Z screen geometry, used for pixel-based cursor positioning. These live here
# because the pixel addressing is itself a DS1000Z-family trait (DHO addresses cursors
# in seconds and never touches them).
_SCREEN_DIVISIONS = 12
_POINTS_PER_DIV = 50          # empirically confirmed: 600 points across 12 divisions
_SCREEN_POINTS = _SCREEN_DIVISIONS * _POINTS_PER_DIV  # 600
_SCREEN_CENTER = _SCREEN_POINTS // 2                  # 300


def screen_x_to_time(scope: pyvisa.resources.Resource, screen_x: int) -> float:
    """Convert a screen X pixel position back to time in seconds."""
    scale = float(scope.query(":TIM:SCAL?").strip())
    offset = float(scope.query(":TIM:OFFS?").strip())
    return (screen_x - _SCREEN_CENTER) * scale / _POINTS_PER_DIV + offset


def time_to_screen_x(scope: pyvisa.resources.Resource, time_s: float) -> int:
    """Convert a time value (seconds) to a screen X position integer for cursor commands.

    Formula derived from DS1000Z geometry (50 pts/div, 12 div, 600 total):
        screen_x = (time - offset) * points_per_div / scale + screen_center
    """
    scale = float(scope.query(":TIM:SCAL?").strip())
    offset = float(scope.query(":TIM:OFFS?").strip())
    screen_x = int(round((time_s - offset) * _POINTS_PER_DIV / scale + _SCREEN_CENTER))
    return max(5, min(594, screen_x))  # DS1000Z cursor range per manual: 5–594


class ScopeDriver:
    """Base class for a Rigol scope family. Subclasses override only what differs.

    The base intentionally leaves the command-producing methods abstract so a partially
    implemented driver fails loudly rather than silently sending DS1000Z commands.
    """

    name: str = "generic"
    # Two-source (delay/phase) item names this family accepts, and aliases mapping the
    # canonical DS1000Z names onto family-specific ones (e.g. RDELAY -> RRDELAY on DHO).
    two_source_items: frozenset[str] = frozenset()
    two_source_aliases: dict[str, str] = {}

    @classmethod
    def matches(cls, idn: str) -> bool:
        """True if this driver handles the scope identified by ``idn`` (an ``*IDN?`` string)."""
        return False

    # --- screenshot ---
    def screenshot_query(self) -> str:
        """The ``:DISPlay:DATA?`` query that returns a PNG TMC block for this family."""
        raise NotImplementedError

    # --- autoscale ---
    def autoscale(self, scope: pyvisa.resources.Resource) -> None:
        """Issue the auto-setup action and block until it completes."""
        raise NotImplementedError

    # --- waveform ---
    def prepare_waveform(self, scope: pyvisa.resources.Resource) -> None:
        """Any setup needed before ``:WAV:PRE?``/``:WAV:DATA?`` (default: nothing)."""

    def read_waveform_data(self, scope: pyvisa.resources.Resource) -> str:
        """Read ``:WAV:DATA?`` and return the CSV payload as a string (header stripped).

        Implementations differ per family: DS1000Z wraps the ASCII payload in an IEEE
        488.2 definite-length block (``#N<len><csv>``) which must be read with the
        backend-aware exact-byte-count reader to avoid hanging USBTMC bulk-IN on
        pyvisa-py/WinUSB; DHO sends bare CSV with no header and can be read to the
        newline terminator (safe because ASCII payloads never contain 0x0A).
        """
        raise NotImplementedError

    # --- cursors ---
    def write_cursor_axis(self, scope: pyvisa.resources.Resource,
                          prefix: str, name: str, value_s: float) -> str:
        """Set cursor axis ``name`` ('A' or 'B') under ``prefix`` to ``value_s`` seconds.

        Returns the command string sent (so the caller can report it in errors).
        """
        raise NotImplementedError

    def read_cursor_axes_s(self, scope: pyvisa.resources.Resource,
                           prefix: str) -> tuple[float, float]:
        """Return the (A, B) cursor X positions in seconds for the given ``prefix``."""
        raise NotImplementedError

    # --- measurement ---
    def register_measure_item(self, scope: pyvisa.resources.Resource,
                              item: str, *sources: str) -> None:
        """Activate ``item`` for the given source channel(s) so the next
        ``:MEASure:ITEM?`` returns its value."""
        raise NotImplementedError

    def resolve_two_source_item(self, item: str) -> str:
        """Map a requested two-source item onto this family's name, validating it.

        Raises ValueError if the (post-alias) item is not supported by this family.
        """
        it = self.two_source_aliases.get(item.upper(), item.upper())
        if it not in self.two_source_items:
            raise ValueError(
                f"'{item}' is not a valid two-source item for {self.name}. "
                f"Valid: {sorted(self.two_source_items)}"
            )
        return it


class DS1000ZDriver(ScopeDriver):
    """Rigol DS1000Z / MSO1000Z series (8-bit)."""

    name = "DS1000Z"
    two_source_items = frozenset({"RDELAY", "FDELAY", "RPHASE", "FPHASE"})

    @classmethod
    def matches(cls, idn: str) -> bool:
        up = idn.upper()
        return "DS1" in up or "MSO1" in up

    def screenshot_query(self) -> str:
        return ":DISPlay:DATA? ON,OFF,PNG"  # color, invert, format

    def autoscale(self, scope: pyvisa.resources.Resource) -> None:
        scope.query(":AUToscale;*OPC?")  # chain OPC? so the query blocks until complete

    def read_waveform_data(self, scope: pyvisa.resources.Resource) -> str:
        # DS1000Z wraps the ASCII CSV in an IEEE 488.2 definite-length block. Use the
        # backend-aware reader: on NI-VISA (@ivi) it reads the full message in one
        # read_raw, on pyvisa-py (@py, used for WinUSB and LAN) it reads by exact byte
        # count. A terminator-based read on @py + USBTMC can hang the bulk-IN endpoint
        # for large blocks and has wedged the scope (observed on DS1054Z / WinUSB).
        from rigol_mcp.scope import _read_definite_block  # lazy: avoid drivers↔scope cycle
        scope.write(":WAV:DATA?")
        return _read_definite_block(scope).decode("ascii")

    def write_cursor_axis(self, scope, prefix, name, value_s):
        cmd = f"{prefix}:{name}X {time_to_screen_x(scope, value_s)}"  # seconds -> pixels
        scope.write(cmd)
        return cmd

    def read_cursor_axes_s(self, scope, prefix):
        ax = screen_x_to_time(scope, int(float(scope.query(f"{prefix}:AX?").strip())))
        bx = screen_x_to_time(scope, int(float(scope.query(f"{prefix}:BX?").strip())))
        return ax, bx

    def register_measure_item(self, scope, item, *sources):
        scope.write(f":MEASure:ITEM {','.join((item, *sources))}")


class DHODriver(ScopeDriver):
    """Rigol DHO series (12-bit)."""

    name = "DHO"
    # DHO has no plain RDELay/FDELay/RPHase/FPHase. It exposes a 4-way matrix combining
    # rising/falling edges on each source. DS1000Z names (rise-to-rise, fall-to-fall) map
    # to the homogeneous pairs; rise-to-fall and fall-to-rise are DHO-only.
    two_source_items = frozenset({
        "RRDELAY", "RFDELAY", "FRDELAY", "FFDELAY",
        "RRPHASE", "RFPHASE", "FRPHASE", "FFPHASE",
    })
    two_source_aliases = {
        "RDELAY": "RRDELAY", "FDELAY": "FFDELAY",
        "RPHASE": "RRPHASE", "FPHASE": "FFPHASE",
    }

    @classmethod
    def matches(cls, idn: str) -> bool:
        return "DHO" in idn.upper()

    def screenshot_query(self) -> str:
        return ":DISPlay:DATA? PNG"  # single param; DHO rejects the 3-param DS1000Z form

    def autoscale(self, scope: pyvisa.resources.Resource) -> None:
        # DHO rejects bare :AUToscale (-109 "Missing parameter"); its action is :AUToset,
        # which does not chain with ;*OPC?, so wait on a separate *OPC?.
        scope.write(":AUToset")
        scope.query("*OPC?")

    def prepare_waveform(self, scope: pyvisa.resources.Resource) -> None:
        # DHO :WAV:STOP defaults to 2, not the full screen buffer, so PRE?/DATA? return a
        # 2-point slice unless the range is set. 1000 is the NORM max; the scope clamps.
        scope.write(":WAV:STAR 1")
        scope.write(":WAV:STOP 1000")

    def read_waveform_data(self, scope: pyvisa.resources.Resource) -> str:
        # DHO returns bare CSV with no IEEE 488.2 block header, unlike DS1000Z. ASCII
        # waveform data never contains 0x0A, so reading to the newline terminator is
        # safe on every backend (no bulk-IN-hang risk that the byte-count reader exists
        # to avoid for binary blocks).
        return scope.query(":WAV:DATA?").strip()

    def write_cursor_axis(self, scope, prefix, name, value_s):
        cmd = f"{prefix}:C{name}X {value_s}"  # :CAX/:CBX take seconds directly
        scope.write(cmd)
        return cmd

    def read_cursor_axes_s(self, scope, prefix):
        ax = float(scope.query(f"{prefix}:CAX?").strip())
        bx = float(scope.query(f"{prefix}:CBX?").strip())
        return ax, bx

    def register_measure_item(self, scope, item, *sources):
        # :MEAS:STATistic:ITEM registers the item persistently without clearing its cached
        # value, so a stopped DHO returns the last acquisition instead of the 9.9E37
        # sentinel that bare :MEAS:ITEM leaves until the next trigger.
        scope.write(f":MEASure:STATistic:ITEM {','.join((item, *sources))}")


# Registry: checked in order, first match wins. An identity matched by no driver is an
# error (see driver_for) — we do not guess a dialect for an unknown instrument.
DS1000Z = DS1000ZDriver()
DHO = DHODriver()
_DRIVERS: tuple[ScopeDriver, ...] = (DHO, DS1000Z)

# Union of every family's two-source items — used to reject two-source items passed to the
# single-source measure(), and to advertise the full enum in the tool schema.
ALL_TWO_SOURCE_ITEMS: frozenset[str] = frozenset().union(
    *(d.two_source_items for d in _DRIVERS)
)


def driver_for(idn: str) -> ScopeDriver:
    """Select the dialect driver for a scope from its ``*IDN?`` string.

    Raises RuntimeError if no registered driver recognises the identity. We refuse to
    guess a dialect for an unknown instrument rather than send it possibly-wrong SCPI.
    """
    for d in _DRIVERS:
        if d.matches(idn):
            return d
    supported = ", ".join(d.name for d in _DRIVERS)
    raise RuntimeError(
        f"Unsupported instrument identity: {idn.strip()!r}. No dialect driver matched "
        f"(supported families: {supported}). Add a ScopeDriver for it in rigol_mcp/drivers.py."
    )
