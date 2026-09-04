-- Core MVP schema (blueprint sections 9, 12, 13).
-- Applied automatically by the postgres container on first startup.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS instruments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    market TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    underlying TEXT,
    expiry TIMESTAMPTZ,
    strike DOUBLE PRECISION,
    option_type TEXT,
    lot_size INTEGER NOT NULL DEFAULT 1,
    tick_size DOUBLE PRECISION NOT NULL DEFAULT 0.05,
    currency TEXT NOT NULL DEFAULT 'INR',
    active BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (symbol, exchange)
);

CREATE TABLE IF NOT EXISTS candles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id UUID NOT NULL REFERENCES instruments(id),
    "timestamp" TIMESTAMPTZ NOT NULL,
    timeframe TEXT NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume DOUBLE PRECISION NOT NULL DEFAULT 0,
    UNIQUE (instrument_id, timeframe, "timestamp")
);

CREATE INDEX IF NOT EXISTS ix_candles_instrument_id ON candles (instrument_id);
CREATE INDEX IF NOT EXISTS ix_candles_timestamp ON candles ("timestamp");
CREATE INDEX IF NOT EXISTS ix_candles_timeframe ON candles (timeframe);
CREATE INDEX IF NOT EXISTS ix_instruments_symbol ON instruments (symbol);
