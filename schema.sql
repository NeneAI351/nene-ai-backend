-- NENE AI production PostgreSQL schema
-- The backend also creates these objects automatically when DATABASE_URL is configured.
-- Keep this file as the reviewable migration baseline for managed Postgres/Supabase.

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY,
  email TEXT UNIQUE,
  display_name TEXT,
  avatar_url TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS projects (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS characters (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  profile JSONB NOT NULL DEFAULT '{}'::jsonb,
  reference_asset_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS generations (
  id UUID PRIMARY KEY,
  user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
  provider TEXT NOT NULL,
  provider_job_id TEXT,
  generation_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  request JSONB NOT NULL,
  result JSONB,
  error JSONB,
  credits_reserved NUMERIC(12,2) NOT NULL DEFAULT 0,
  credits_charged NUMERIC(12,2) NOT NULL DEFAULT 0,
  credits_refunded NUMERIC(12,2) NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS credit_ledger (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  generation_id UUID REFERENCES generations(id) ON DELETE SET NULL,
  entry_type TEXT NOT NULL,
  amount NUMERIC(12,2) NOT NULL,
  balance_after NUMERIC(12,2),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  external_customer_id TEXT,
  external_subscription_id TEXT,
  plan_id TEXT NOT NULL,
  status TEXT NOT NULL,
  current_period_start TIMESTAMPTZ,
  current_period_end TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(provider, external_subscription_id)
);

CREATE TABLE IF NOT EXISTS provider_costs (
  id UUID PRIMARY KEY,
  generation_id UUID REFERENCES generations(id) ON DELETE SET NULL,
  provider TEXT NOT NULL,
  model TEXT,
  units NUMERIC(12,4) NOT NULL DEFAULT 0,
  unit_type TEXT,
  cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS generation_idempotency (
  id UUID PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,
  response JSONB,
  generation_id UUID REFERENCES generations(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);


-- Server-side wallet and idempotent wallet operation records.
CREATE TABLE IF NOT EXISTS wallets (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  available_credits NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (available_credits >= 0),
  reserved_credits NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (reserved_credits >= 0),
  lifetime_purchased NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (lifetime_purchased >= 0),
  lifetime_bonus NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (lifetime_bonus >= 0),
  lifetime_consumed NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (lifetime_consumed >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallet_operations (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  operation_key TEXT NOT NULL,
  operation_type TEXT NOT NULL,
  amount NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  generation_id UUID REFERENCES generations(id) ON DELETE SET NULL,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, operation_key)
);

CREATE INDEX IF NOT EXISTS idx_wallet_operations_user_created
  ON wallet_operations(user_id, created_at DESC);
ALTER TABLE credit_ledger ADD COLUMN IF NOT EXISTS operation_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_ledger_user_operation
  ON credit_ledger(user_id, operation_key) WHERE operation_key IS NOT NULL;
