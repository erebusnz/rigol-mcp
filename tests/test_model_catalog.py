"""Validate the external catalog before its entries reach hardware operations."""

from dataclasses import FrozenInstanceError

import pytest

from rigol_mcp.models import _parse_catalog


def catalog(**entry):
    return {"models": {"DHO802": {"family": "DHO", "analog_channel_count": 2, **entry}}}


def test_catalog_produces_immutable_snapshots():
    data = catalog()
    data["aliases"] = {"TEST-ALIAS": "DHO802"}
    models, aliases = _parse_catalog(data)
    data["models"]["DHO802"]["analog_channel_count"] = 4
    data["aliases"]["TEST-ALIAS"] = "MISSING"
    assert models["DHO802"].analog_channels == ("CHAN1", "CHAN2")
    assert aliases["TEST-ALIAS"] == "DHO802"
    with pytest.raises(TypeError):
        models["OTHER"] = models["DHO802"]
    with pytest.raises(TypeError):
        aliases["OTHER"] = "DHO802"
    with pytest.raises(FrozenInstanceError):
        models["DHO802"].analog_channel_count = 4


@pytest.mark.parametrize("count", [0, -1, True, 2.5, "2"])
def test_catalog_rejects_invalid_channel_counts(count):
    with pytest.raises(ValueError, match="positive integer"):
        _parse_catalog(catalog(analog_channel_count=count))


@pytest.mark.parametrize("family", ["", "dho", " DHO", 42])
def test_catalog_rejects_invalid_families(family):
    with pytest.raises(ValueError, match="invalid family"):
        _parse_catalog(catalog(family=family))


@pytest.mark.parametrize("name", ["", "dho802", "DHO802 ", "DHO,802"])
def test_catalog_rejects_unreachable_model_names(name):
    entry = catalog()["models"]["DHO802"]
    with pytest.raises(ValueError, match="invalid model entry"):
        _parse_catalog({"models": {name: entry}})


@pytest.mark.parametrize("entry", [
    {"family": "DHO"},
    {"analog_channel_count": 2},
    {"family": "DHO", "analog_channel_count": 2, "typo": True},
    "DHO802",
])
def test_catalog_rejects_missing_or_unexpected_model_fields(entry):
    with pytest.raises(ValueError, match="invalid model entry"):
        _parse_catalog({"models": {"DHO802": entry}})


@pytest.mark.parametrize("data", [
    {}, {"models": {}}, {"models": []},
    {**catalog(), "aliases": []}, {**catalog(), "typo": {}},
])
def test_catalog_rejects_empty_or_malformed_tables(data):
    with pytest.raises(ValueError, match="models.toml"):
        _parse_catalog(data)


@pytest.mark.parametrize("aliases", [
    {"ALIAS": "MISSING"},
    {"ALIAS": "CHAIN", "CHAIN": "DHO802"},
    {"ALIAS": 2},
    {"DHO802": "DHO802"},
    {"alias": "DHO802"},
])
def test_catalog_rejects_dangling_chained_or_conflicting_aliases(aliases):
    with pytest.raises(ValueError, match="alias"):
        _parse_catalog({**catalog(), "aliases": aliases})
