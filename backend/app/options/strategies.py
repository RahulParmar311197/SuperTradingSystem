"""Options strategy construction (blueprint §37): builds the leg list for a
named strategy from a simple strike->premium chain. Bias-appropriate
strategy selection (bullish/bearish/neutral) and risk-profile filtering are
the caller's responsibility — this module only knows how to assemble legs
correctly once a strategy has been chosen.
"""

from __future__ import annotations

import inspect

from app.database.models.strategy import Direction
from app.options.greeks import OptionType
from app.options.payoff import OptionLeg

OptionChain = dict[float, dict[str, float]]  # {strike: {"CALL": premium, "PUT": premium}}


def _leg(chain: OptionChain, strike: float, option_type: OptionType, direction: Direction, quantity: float, lot_size: int) -> OptionLeg:
    premium = chain[strike][option_type.value]
    return OptionLeg(option_type=option_type, strike=strike, premium=premium, quantity=quantity, direction=direction, lot_size=lot_size)


def long_call(chain: OptionChain, strike: float, quantity: float = 1, lot_size: int = 1) -> list[OptionLeg]:
    return [_leg(chain, strike, OptionType.CALL, Direction.LONG, quantity, lot_size)]


def long_put(chain: OptionChain, strike: float, quantity: float = 1, lot_size: int = 1) -> list[OptionLeg]:
    return [_leg(chain, strike, OptionType.PUT, Direction.LONG, quantity, lot_size)]


def bull_call_spread(chain: OptionChain, long_strike: float, short_strike: float, quantity: float = 1, lot_size: int = 1) -> list[OptionLeg]:
    if not long_strike < short_strike:
        raise ValueError("bull_call_spread requires long_strike < short_strike")
    return [
        _leg(chain, long_strike, OptionType.CALL, Direction.LONG, quantity, lot_size),
        _leg(chain, short_strike, OptionType.CALL, Direction.SHORT, quantity, lot_size),
    ]


def bear_put_spread(chain: OptionChain, long_strike: float, short_strike: float, quantity: float = 1, lot_size: int = 1) -> list[OptionLeg]:
    if not long_strike > short_strike:
        raise ValueError("bear_put_spread requires long_strike > short_strike")
    return [
        _leg(chain, long_strike, OptionType.PUT, Direction.LONG, quantity, lot_size),
        _leg(chain, short_strike, OptionType.PUT, Direction.SHORT, quantity, lot_size),
    ]


def bull_put_spread(chain: OptionChain, short_strike: float, long_strike: float, quantity: float = 1, lot_size: int = 1) -> list[OptionLeg]:
    """Credit spread: sell a higher-strike put, buy a lower-strike put."""
    if not short_strike > long_strike:
        raise ValueError("bull_put_spread requires short_strike > long_strike")
    return [
        _leg(chain, short_strike, OptionType.PUT, Direction.SHORT, quantity, lot_size),
        _leg(chain, long_strike, OptionType.PUT, Direction.LONG, quantity, lot_size),
    ]


def bear_call_spread(chain: OptionChain, short_strike: float, long_strike: float, quantity: float = 1, lot_size: int = 1) -> list[OptionLeg]:
    """Credit spread: sell a lower-strike call, buy a higher-strike call."""
    if not short_strike < long_strike:
        raise ValueError("bear_call_spread requires short_strike < long_strike")
    return [
        _leg(chain, short_strike, OptionType.CALL, Direction.SHORT, quantity, lot_size),
        _leg(chain, long_strike, OptionType.CALL, Direction.LONG, quantity, lot_size),
    ]


def short_straddle(chain: OptionChain, atm_strike: float, quantity: float = 1, lot_size: int = 1) -> list[OptionLeg]:
    return [
        _leg(chain, atm_strike, OptionType.CALL, Direction.SHORT, quantity, lot_size),
        _leg(chain, atm_strike, OptionType.PUT, Direction.SHORT, quantity, lot_size),
    ]


def short_strangle(chain: OptionChain, put_strike: float, call_strike: float, quantity: float = 1, lot_size: int = 1) -> list[OptionLeg]:
    if not put_strike < call_strike:
        raise ValueError("short_strangle requires put_strike < call_strike")
    return [
        _leg(chain, put_strike, OptionType.PUT, Direction.SHORT, quantity, lot_size),
        _leg(chain, call_strike, OptionType.CALL, Direction.SHORT, quantity, lot_size),
    ]


def iron_condor(
    chain: OptionChain,
    put_long_strike: float,
    put_short_strike: float,
    call_short_strike: float,
    call_long_strike: float,
    quantity: float = 1,
    lot_size: int = 1,
) -> list[OptionLeg]:
    if not (put_long_strike < put_short_strike < call_short_strike < call_long_strike):
        raise ValueError("iron_condor requires put_long < put_short < call_short < call_long")
    return [
        _leg(chain, put_long_strike, OptionType.PUT, Direction.LONG, quantity, lot_size),
        _leg(chain, put_short_strike, OptionType.PUT, Direction.SHORT, quantity, lot_size),
        _leg(chain, call_short_strike, OptionType.CALL, Direction.SHORT, quantity, lot_size),
        _leg(chain, call_long_strike, OptionType.CALL, Direction.LONG, quantity, lot_size),
    ]


def iron_butterfly(
    chain: OptionChain,
    put_long_strike: float,
    atm_strike: float,
    call_long_strike: float,
    quantity: float = 1,
    lot_size: int = 1,
) -> list[OptionLeg]:
    if not (put_long_strike < atm_strike < call_long_strike):
        raise ValueError("iron_butterfly requires put_long < atm < call_long")
    return [
        _leg(chain, put_long_strike, OptionType.PUT, Direction.LONG, quantity, lot_size),
        _leg(chain, atm_strike, OptionType.PUT, Direction.SHORT, quantity, lot_size),
        _leg(chain, atm_strike, OptionType.CALL, Direction.SHORT, quantity, lot_size),
        _leg(chain, call_long_strike, OptionType.CALL, Direction.LONG, quantity, lot_size),
    ]


_STRATEGY_BUILDERS = {
    "long_call": long_call,
    "long_put": long_put,
    "bull_call_spread": bull_call_spread,
    "bear_put_spread": bear_put_spread,
    "bull_put_spread": bull_put_spread,
    "bear_call_spread": bear_call_spread,
    "short_straddle": short_straddle,
    "short_strangle": short_strangle,
    "iron_condor": iron_condor,
    "iron_butterfly": iron_butterfly,
}

# Bias -> strategies permitted, per blueprint §37.
BIAS_STRATEGIES = {
    "bullish": ["long_call", "bull_call_spread", "bull_put_spread"],
    "bearish": ["long_put", "bear_put_spread", "bear_call_spread"],
    "neutral": ["iron_condor", "iron_butterfly", "short_straddle", "short_strangle"],
}


def required_arguments(name: str) -> list[str]:
    """The strike arguments `name`'s builder cannot be called without.

    Every one of the ten builders takes at least one -- `long_call` needs a
    `strike`, `iron_condor` needs four -- and nothing told a caller which.
    `POST /options/strategy` spreads a caller-supplied `strategy_kwargs`
    into the builder, and that field's **default is an empty dict**, so the
    documented default request could not succeed for any strategy: it
    raised `TypeError` (missing positional arguments), which the route did
    not catch, and came back as HTTP 500 with a traceback.

    Read from the signature rather than hard-coded, so a builder that gains
    or loses an argument cannot drift away from what the API reports.
    """
    builder = _STRATEGY_BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"Unknown options strategy: {name}")
    return [
        parameter.name
        for parameter in inspect.signature(builder).parameters.values()
        if parameter.default is inspect.Parameter.empty and parameter.name != "chain"
    ]


# Supplied by `build_strategy`'s own caller, so passing any of them again
# through a request's `strategy_kwargs` is "multiple values for argument"
# -- another `TypeError`, and another 500. Enforced at the API layer, which
# is the only place that can still tell the request's copy from the
# route's; see the note in `build_strategy`.
RESERVED_ARGUMENTS = frozenset({"chain", "quantity", "lot_size"})


def build_strategy(name: str, chain: OptionChain, **kwargs) -> list[OptionLeg]:
    builder = _STRATEGY_BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"Unknown options strategy: {name}")

    # Turn every way of calling this wrongly into a `ValueError` naming
    # what the strategy actually wants. The builders raise `KeyError` for a
    # strike that is not in the chain and `ValueError` for a bad one, both
    # of which callers already handle; a `TypeError` out of `**kwargs` was
    # the one that escaped as a 500.
    required = required_arguments(name)
    accepted = {p.name for p in inspect.signature(builder).parameters.values()}
    missing = [argument for argument in required if argument not in kwargs]
    if missing:
        raise ValueError(
            f"Options strategy {name!r} requires {required}; missing {missing}. "
            "Pass them in `strategy_kwargs`."
        )
    # NOT the place to reject `quantity`/`lot_size`: this function's own
    # caller passes them as keyword arguments, so by the time they land in
    # `**kwargs` they are indistinguishable from ones a request smuggled in
    # -- rejecting them here refused every correct call. The API layer can
    # tell the two apart, because it holds `strategy_kwargs` separately,
    # and `RESERVED_ARGUMENTS` is exported for it to use.
    unknown = sorted(set(kwargs) - accepted)
    if unknown:
        raise ValueError(f"Options strategy {name!r} does not accept {unknown}; it accepts {required}.")

    return builder(chain, **kwargs)
