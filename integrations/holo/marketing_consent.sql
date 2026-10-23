-- ============================================================================
-- Proposed migration for the Holo app (run in Holo's Supabase, not here).
--
-- Holo has no marketing consent today, so the growth engine treats every Holo
-- user as "never asked" and syncs nobody. This table records each change to a
-- user's marketing-email toggle. It is append-only: the latest row per user is
-- the current choice, and the history is the audit trail.
--
-- The app writes a row when the user flips the toggle in Profile settings.
-- The growth engine reads it with the service key (read-only).
-- ============================================================================

create table if not exists marketing_consents (
  id           bigint generated always as identity primary key,
  user_id      uuid not null references auth.users(id) on delete cascade,
  granted      boolean not null,
  source       text not null default 'profile_toggle',
  recorded_at  timestamptz not null default now()
);

create index if not exists marketing_consents_user_idx
  on marketing_consents (user_id, recorded_at desc);

alter table marketing_consents enable row level security;

-- Users may read their own history and add rows for themselves only.
drop policy if exists "own consent select" on marketing_consents;
create policy "own consent select" on marketing_consents
  for select using (auth.uid() = user_id);

drop policy if exists "own consent insert" on marketing_consents;
create policy "own consent insert" on marketing_consents
  for insert with check (auth.uid() = user_id);

-- No update or delete policies: history is never rewritten.
