-- Run as the PostgreSQL database owner, connected to the sub2api database.
-- Create credentials separately; never commit the real password.
-- CREATE ROLE key_status_ro LOGIN PASSWORD '<generated password>';

ALTER ROLE key_status_ro SET default_transaction_read_only = on;
ALTER ROLE key_status_ro SET statement_timeout = '4s';
ALTER ROLE key_status_ro SET log_parameter_max_length_on_error = 0;
-- psycopg2 binds values safely but PostgreSQL can log the resulting SQL text.
-- These per-login settings require a superuser and suppress statement logging.
ALTER ROLE key_status_ro SET log_min_error_statement = 'panic';
ALTER ROLE key_status_ro SET log_statement = 'none';
ALTER ROLE key_status_ro SET log_min_duration_statement = -1;
ALTER ROLE key_status_ro SET log_min_duration_sample = -1;
ALTER ROLE key_status_ro SET log_duration = off;
GRANT CONNECT ON DATABASE sub2api TO key_status_ro;
GRANT USAGE ON SCHEMA public TO key_status_ro;

GRANT SELECT (
    id, user_id, group_id, key, status, last_used_at, expires_at,
    quota, quota_used, deleted_at,
    rate_limit_5h, rate_limit_1d, rate_limit_7d,
    usage_5h, usage_1d, usage_7d,
    window_5h_start, window_1d_start, window_7d_start
) ON public.api_keys TO key_status_ro;

GRANT SELECT (
    id, status, balance, frozen_balance, concurrency, deleted_at
) ON public.users TO key_status_ro;

GRANT SELECT (
    id, name, status, subscription_type, deleted_at,
    daily_limit_usd, weekly_limit_usd, monthly_limit_usd
) ON public.groups TO key_status_ro;

GRANT SELECT (
    id, user_id, group_id, status, starts_at, expires_at, deleted_at,
    daily_window_start, weekly_window_start, monthly_window_start,
    daily_usage_usd, weekly_usage_usd, monthly_usage_usd
) ON public.user_subscriptions TO key_status_ro;

GRANT SELECT (
    api_key_id, actual_cost, input_tokens, output_tokens,
    cache_creation_tokens, cache_read_tokens
) ON public.usage_logs TO key_status_ro;
