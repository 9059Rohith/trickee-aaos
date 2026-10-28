import pytest

from trickee.providers import MockMapProvider, map_provider


def test_mock_map_provider_labels_every_result():
    provider = MockMapProvider()
    assert provider.geocode("Bengaluru")["quality"] == "SIMULATED"
    route = provider.route("Bengaluru", "Hosur")
    assert route.distance_km > 20
    assert route.source == "SIMULATED"
    assert provider.traffic("Bengaluru", "Hosur")["live"] is False
    assert provider.map_tile(1, 1, 1)["status"] == "NOT_SUPPORTED"
    assert provider.search("my")
    with pytest.raises(ValueError):
        provider.route("Bengaluru", "unknown")
    with pytest.raises(ValueError):
        map_provider("unconfigured-real-provider")
