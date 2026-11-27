"""Documented model capabilities, independent of implemented SCPI dialects.

Only explicit model names are accepted; serial numbers and model-number digits
are never used to guess capabilities. Source tables are linked in README.md.
"""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ModelCapabilities:
    model: str
    family: str
    analog_channel_count: int

    @property
    def analog_channels(self) -> tuple[str, ...]:
        return tuple(f"CHAN{i}" for i in range(1, self.analog_channel_count + 1))


# Exact names from manufacturer model tables. A family entry does not imply
# that a SCPI driver or acquisition-memory export is implemented for that family.
_MODEL_GROUPS = (
    ("DS1000Z", 4, (
        "DS1054Z", "DS1074Z", "DS1104Z", "DS1074Z-S", "DS1104Z-S",
        "MSO1074Z", "MSO1104Z", "MSO1074Z-S", "MSO1104Z-S",
        "DS1074Z PLUS", "DS1074Z-S PLUS", "DS1104Z PLUS", "DS1104Z-S PLUS",
    )),
    ("DHO", 2, ("DHO802", "DHO812", "DHO1072", "DHO1102", "DHO1202")),
    ("DHO", 4, (
        "DHO804", "DHO814", "DHO824", "DHO914", "DHO914S", "DHO924", "DHO924S",
        "DHO1074", "DHO1104", "DHO1204", "DHO4204", "DHO4404", "DHO4804",
    )),
    ("DS2000A", 2, (
        "DS2072A", "DS2072A-S", "DS2102A", "DS2102A-S",
        "DS2202A", "DS2202A-S", "DS2302A", "DS2302A-S",
        "MSO2072A", "MSO2072A-S", "MSO2102A", "MSO2102A-S",
        "MSO2202A", "MSO2202A-S", "MSO2302A", "MSO2302A-S",
    )),
)

MODEL_CAPABILITIES = MappingProxyType({
    model: ModelCapabilities(model, family, count)
    for family, count, models in _MODEL_GROUPS
    for model in models
})

# Rigol also publishes these Plus names with a hyphen. No other suffixes
# are stripped or inferred: each spelling must be explicitly documented.
_MODEL_ALIASES = {
    "DS1074Z-PLUS": "DS1074Z PLUS",
    "DS1074Z-S-PLUS": "DS1074Z-S PLUS",
    "DS1104Z-PLUS": "DS1104Z PLUS",
    "DS1104Z-S-PLUS": "DS1104Z-S PLUS",
}

# Tool schemas describe the union of known channels without opening hardware.
# Each channel operation also validates against the connected model at runtime.
ANALOG_CHANNELS = tuple(f"CHAN{i}" for i in range(
    1, max(model.analog_channel_count for model in MODEL_CAPABILITIES.values()) + 1
))


def capabilities_for(idn: str) -> ModelCapabilities:
    """Look up the model field in a Rigol *IDN? response, without device I/O."""
    fields = [field.strip().upper() for field in idn.split(",")]
    if len(fields) >= 2 and fields[0] in ("RIGOL", "RIGOL TECHNOLOGIES"):
        model = _MODEL_ALIASES.get(fields[1], fields[1])
        capabilities = MODEL_CAPABILITIES.get(model)
        if capabilities is not None:
            return capabilities
    raise RuntimeError(
        f"Unsupported instrument identity: {idn.strip()!r}. "
        "No documented model capabilities found; add the exact model to rigol_mcp/models.py."
    )
