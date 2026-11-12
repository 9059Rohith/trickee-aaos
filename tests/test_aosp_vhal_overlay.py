"""Check the simulator overlay against the exact pinned AOSP VHAL config."""
import json
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "aosp" / "trickee-vhal" / "TrickeeInitialValues.json"
UPSTREAM = (ROOT / "aosp" / "upstream" / "hardware-interfaces" /
            "automotive" / "vehicle" / "aidl" / "impl" / "current" /
            "default_config" / "config" / "DefaultProperties.json")


def test_fake_vhal_overlay_has_expected_properties_and_units():
    override = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert override["apiVersion"] == 2
    supplied = {entry["property"]: entry for entry in override["properties"]}
    assert len(supplied) == len(override["properties"])
    assert set(supplied) == {
        "VehicleProperty::INFO_EV_BATTERY_CAPACITY",
        "VehicleProperty::EV_BATTERY_LEVEL",
        "VehicleProperty::PERF_VEHICLE_SPEED",
    }
    capacity_wh = supplied["VehicleProperty::INFO_EV_BATTERY_CAPACITY"]["defaultValue"]["floatValues"][0]
    level_wh = supplied["VehicleProperty::EV_BATTERY_LEVEL"]["defaultValue"]["floatValues"][0]
    assert capacity_wh == 65000 and level_wh == 41600
    assert level_wh / capacity_wh * 100 == 64
    assert supplied["VehicleProperty::PERF_VEHICLE_SPEED"]["defaultValue"]["floatValues"] == [0]


def test_fake_vhal_overlay_uses_pinned_upstream_properties():
    if not UPSTREAM.is_file():
        pytest.skip("Pinned AOSP checkout is unavailable; run aosp/fetch-upstream.ps1 to verify upstream compatibility")
    override = json.loads(CONFIG.read_text(encoding="utf-8"))
    upstream = json.loads(UPSTREAM.read_text(encoding="utf-8"))
    assert override["apiVersion"] == upstream["apiVersion"] == 2
    supplied = {entry["property"]: entry for entry in override["properties"]}
    defaults = {entry["property"]: entry for entry in upstream["properties"]}
    assert set(supplied) <= set(defaults)
    for name, entry in supplied.items():
        assert set(entry) <= set(defaults[name])
        if "maxSampleRate" in entry:
            assert entry["maxSampleRate"] == defaults[name]["maxSampleRate"]
        if "minSampleRate" in entry:
            assert entry["minSampleRate"] == defaults[name]["minSampleRate"]


def test_product_fragment_installs_override_into_vendor_image():
    fragment = (CONFIG.parent / "TrickeeVehicle.mk").read_text(encoding="utf-8")
    assert "$(TARGET_COPY_OUT_VENDOR)/etc/automotive/vhaloverride/TrickeeInitialValues.json" in fragment
    assert "persist.vendor.vhal_init_value_override=true" in fragment
