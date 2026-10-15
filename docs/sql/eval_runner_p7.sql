-- Least-privilege role for the current app/v2 P7 pilot roster.
-- DBA: apply after migrations are at 20260910_0008, connected to database
-- `evaluation` as a role allowed to create roles and grant these privileges:
--   psql -h 127.0.0.1 -p 55433 -U postgres -d evaluation -v ON_ERROR_STOP=1 -f docs/sql/eval_runner_p7.sql
-- Then set the secret interactively in psql (do not put it in this file):
--   \password eval_runner
-- The r8 launcher defaults to this URL (no password embedded):
--   postgresql+psycopg://eval_runner@127.0.0.1:55433/evaluation
-- Set EVALUATION_DATABASE_URL only when the DBA provides a different endpoint;
-- the launcher rejects URLs whose username is not `eval_runner`.
-- Use a NEW run_id for each run: v3 derives conversation/fixture IDs from it.
-- This avoids granting DELETE on prior-run state. The roster has seven read-only
-- cases and one unconfirmed checkout proposal; it does not execute an order.
-- Review this file again if the P7 scope/case roster changes.

CREATE ROLE eval_runner
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION;

GRANT CONNECT ON DATABASE evaluation TO eval_runner;
GRANT USAGE ON SCHEMA public TO eval_runner;

-- Read-only catalog and published knowledge snapshot.
GRANT SELECT ON TABLE
    public.products,
    public.shops,
    public.reviews,
    public.dataset_sources,
    public.v2_knowledge_corpus_versions,
    public.v2_knowledge_index_manifests,
    public.v2_knowledge_documents,
    public.v2_knowledge_chunks,
    public.v2_knowledge_vectors,
    public.v2_book_mappings
TO eval_runner;

-- Durable conversation state, turns, steps, and preference memory.
GRANT SELECT, INSERT, UPDATE ON TABLE
    public.v2_conversations,
    public.v2_turns,
    public.v2_step_results,
    public.v2_preferences
TO eval_runner;

-- P7 seeds missing demo offers; the one checkout case also creates its cart.
GRANT SELECT, INSERT ON TABLE public.v2_offers TO eval_runner;
GRANT SELECT, INSERT ON TABLE
    public.v2_carts,
    public.v2_cart_lines
TO eval_runner;

-- Checkout proposal handling uses SELECT ... FOR UPDATE on these tables.
-- PostgreSQL requires UPDATE on at least one column for row locking; grant only
-- the non-business timestamp column so the pilot cannot change cart or stock data.
GRANT UPDATE (updated_at) ON TABLE
    public.v2_carts,
    public.v2_cart_lines,
    public.v2_offers
TO eval_runner;

-- The checkout fixture checks for prior orders; the unconfirmed proposal is stored.
GRANT SELECT ON TABLE public.v2_orders TO eval_runner;
GRANT SELECT, INSERT ON TABLE public.v2_proposals TO eval_runner;

-- The runtime creates the global account/scopes and records provider attempts.
GRANT SELECT, INSERT, UPDATE ON TABLE
    public.v2_budget_accounts,
    public.v2_budget_scopes,
    public.v2_provider_attempts
TO eval_runner;
