"""Documented model capabilities, independent of implemented SCPI dialects.

Load the packaged models.toml catalog once. Only explicit model names are
accepted; serial numbers and model-number digits never imply capabilities.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
import tomllib
from types import MappingProxyType


@dataclass(frozen=True)
class ModelCapabilities:
    model: str
    family: str
    analog_channel_count: int

    @property
    def analog_channels(self) -> tuple[str, ...]:
        return tuple(f"CHAN{i}" for i in range(1, self.analog_channel_count + 1))


def _catalog_name(value: object) -> bool:
    return (isinstance(value, str) and bool(value)
            and value == value.strip().upper() and "," not in value)


def _parse_catalog(data: dict) -> tuple[Mapping[str, ModelCapabilities], Mapping[str, str]]:
    """Reject malformed catalog entries before they can define tool capabilities."""
    if set(data) - {"models", "aliases"}:
        raise ValueError("models.toml only accepts [models] and [aliases] tables")
    models = data.get("models")
    aliases = data.get("aliases", {})
    if not isinstance(models, dict) or not models or not isinstance(aliases, dict):
        raise ValueError("models.toml requires a nonempty [models] table and an optional [aliases] table")

    capabilities = {}
    for model, entry in models.items():
        if (not _catalog_name(model) or not isinstance(entry, dict)
                or set(entry) != {"family", "analog_channel_count"}):
            raise ValueError(f"models.toml: invalid model entry {model!r}")
        family, count = entry["family"], entry["analog_channel_count"]
        if not _catalog_name(family):
            raise ValueError(f"models.toml: invalid family for {model!r}")
        if type(count) is not int or count < 1:
            raise ValueError(f"models.toml: analog_channel_count for {model!r} must be a positive integer")
        capabilities[model] = ModelCapabilities(model, family, count)

    for alias, target in aliases.items():
        if not _catalog_name(alias) or alias in capabilities:
            raise ValueError(f"models.toml: invalid or conflicting alias {alias!r}")
        if not isinstance(target, str) or target not in capabilities:
            raise ValueError(f"models.toml: alias {alias!r} must target a canonical model")
    return MappingProxyType(capabilities), MappingProxyType(dict(aliases))


def _load_catalog() -> tuple[Mapping[str, ModelCapabilities], Mapping[str, str]]:
    with files("rigol_mcp").joinpath("models.toml").open("rb") as catalog:
        return _parse_catalog(tomllib.load(catalog))


MODEL_CAPABILITIES, _MODEL_ALIASES = _load_catalog()

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
        "No documented model capabilities found; add the exact model to rigol_mcp/models.toml."
    )
