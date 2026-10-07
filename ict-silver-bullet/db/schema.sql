-- I will apply this manually in the Supabase SQL editor; do not connect to a live database from this environment.
create table candles (
  id bigserial primary key, pair text, timeframe text,
  open numeric, high numeric, low numeric, close numeric,
  ts timestamptz, ingested_at timestamptz default now()
);

create table signals (
  id bigserial primary key, pair text, direction text,
  entry numeric, stop_loss numeric, take_profit numeric,
  -- Addition to the original plan: Brain risk engine computes lot_size for Executor order placement.
  lot_size numeric,
  confluence_score int, killzone text, status text default 'pending',
  llm_verdict text, llm_conviction numeric, llm_reasoning text,
  detected_at timestamptz default now()
);

create table trades (
  id bigserial primary key, signal_id bigint references signals(id),
  leg text, entry_fill numeric, exit_fill numeric, lots numeric,
  realized_r numeric, realized_usd numeric,
  opened_at timestamptz, closed_at timestamptz, status text
);

create table equity_snapshots (
  id bigserial primary key, equity numeric, balance numeric,
  daily_dd_pct numeric, overall_dd_pct numeric, ts timestamptz default now()
);

create table news_events (
  id bigserial primary key, title text, currency text, impact text,
  scheduled_at timestamptz, actual text, forecast text, previous text
);

create index signals_status_detected_at_idx on signals(status, detected_at);
-- The unique index also serves the requested candle lookup index.
create unique index candles_pair_timeframe_ts_idx on candles(pair, timeframe, ts);
create index equity_snapshots_ts_idx on equity_snapshots(ts);
create index news_events_scheduled_at_idx on news_events(scheduled_at);

-- NOTE: These tables are service-key-only; RLS denies public clients access.
alter table candles enable row level security;
alter table signals enable row level security;
alter table trades enable row level security;
alter table equity_snapshots enable row level security;
alter table news_events enable row level security;
