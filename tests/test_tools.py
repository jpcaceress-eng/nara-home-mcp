import pytest

from app.security import NaraSecurityError
from app.tools import brightness_pct_to_ha


def test_brightness_rejects_out_of_range_values() -> None:
    with pytest.raises(NaraSecurityError):
        brightness_pct_to_ha(0)
    with pytest.raises(NaraSecurityError):
        brightness_pct_to_ha(101)


def test_brightness_converts_to_home_assistant_scale() -> None:
    assert brightness_pct_to_ha(25) == 64
    assert brightness_pct_to_ha(100) == 255
