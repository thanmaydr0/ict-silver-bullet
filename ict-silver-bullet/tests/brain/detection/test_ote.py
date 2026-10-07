import pytest

from brain.detection.ote import calculate_ote_zone, price_in_ote


@pytest.mark.parametrize("start,end,expected", [
    (100, 200, (121, 138.2)), (200, 100, (161.8, 179)), (100, 100, (100, 100)),
])
def test_hand_computed_retracement(start, end, expected):
    assert calculate_ote_zone(start, end) == pytest.approx(expected)


@pytest.mark.parametrize("price,inside", [(121, True), (130, True), (138.2, True),
                                         (120.99, False), (138.21, False)])
def test_inclusive_band(price, inside):
    assert price_in_ote(price, (121, 138.2)) is inside
