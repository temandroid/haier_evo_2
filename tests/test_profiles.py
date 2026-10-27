"""Device profiles: every YAML loads, our models resolve to their own file."""
from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.util.yaml import load_yaml

from custom_components.haier_evo import config as CFG

DEVICES = Path(CFG.__file__).parent / "devices"


@pytest.mark.parametrize("path", sorted(DEVICES.glob("*.yaml")), ids=lambda p: p.stem)
def test_profile_is_valid(path: Path):
    data = load_yaml(str(path))
    assert isinstance(data, dict)
    for attr in data["attributes"]:
        assert "name" in attr and "id" in attr


@pytest.mark.parametrize(("raw_model", "expected"), [
    ("AS25PHP3HRA", "AS25PHP3HRA"),
    ("AS70S2SF2FA-B", "AS70S2SF2FA"),
])
def test_our_models_have_profiles(raw_model: str, expected: str):
    model = raw_model.replace("-", "").replace("/", "")[:11]
    assert model == expected
    config = CFG.HaierACConfig(model, "")
    assert config._find_config_file().endswith(f"{expected}.yaml")
    assert config.command_name == "4"


def test_unknown_model_uses_default():
    config = CFG.HaierACConfig("XX00UNKNOWN", "")
    assert config._find_config_file().endswith("default.yaml")


def test_item_lookup_returns_none_for_unknown():
    attr = CFG.Attribute({
        "name": "2",
        "description": "Режимы",
        "list": {"data": [{"data": "1", "name": "Охлаждение"}]},
    })
    assert attr.get_item_name("1") == "cool"
    assert attr.get_item_name("99") is None
    assert attr.get_item_code("cool") == "1"
    assert attr.get_item_code("turbo") is None


def test_temperature_items_do_not_pollute_shared_mappings():
    CFG.Item.create("fridge_mode", {"data": "3", "name": "3℃"})
    assert "3℃" not in CFG.Item.mappings
