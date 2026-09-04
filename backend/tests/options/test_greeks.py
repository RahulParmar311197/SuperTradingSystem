import pytest

from app.options.greeks import OptionType, black_scholes_greeks, black_scholes_price


def test_atm_call_delta_is_near_half():
    greeks = black_scholes_greeks(spot=100, strike=100, time_to_expiry=0.25, rate=0.05, iv=0.2, option_type=OptionType.CALL)
    assert 0.45 < greeks.delta < 0.65


def test_atm_put_delta_is_near_negative_half():
    greeks = black_scholes_greeks(spot=100, strike=100, time_to_expiry=0.25, rate=0.05, iv=0.2, option_type=OptionType.PUT)
    assert -0.65 < greeks.delta < -0.35


def test_deep_itm_call_delta_approaches_one():
    greeks = black_scholes_greeks(spot=200, strike=100, time_to_expiry=0.25, rate=0.05, iv=0.2, option_type=OptionType.CALL)
    assert greeks.delta > 0.95


def test_deep_otm_put_delta_approaches_zero():
    greeks = black_scholes_greeks(spot=200, strike=100, time_to_expiry=0.25, rate=0.05, iv=0.2, option_type=OptionType.PUT)
    assert greeks.delta > -0.05


def test_call_put_parity_holds_approximately():
    call = black_scholes_price(100, 100, 0.5, 0.05, 0.2, OptionType.CALL)
    put = black_scholes_price(100, 100, 0.5, 0.05, 0.2, OptionType.PUT)
    # C - P = S - K*e^(-rT)
    import math

    expected = 100 - 100 * math.exp(-0.05 * 0.5)
    assert call - put == pytest.approx(expected, abs=1e-6)


def test_gamma_and_vega_are_positive_for_both_types():
    call = black_scholes_greeks(100, 100, 0.5, 0.05, 0.2, OptionType.CALL)
    put = black_scholes_greeks(100, 100, 0.5, 0.05, 0.2, OptionType.PUT)
    assert call.gamma > 0 and put.gamma > 0
    assert call.vega > 0 and put.vega > 0
