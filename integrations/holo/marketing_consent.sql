-- ============================================================================
-- Migration for the Holo app's Supabase project (not applied by this repository).
--
-- Append-only log of marketing-email consent changes. The latest row per user
-- is the current choice; earlier rows form the audit trail. Written by the app
-- when the user changes the setting in Profile; read by the growth engine with
-- the service key. Until this table exists, all users are treated as not opted in.
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
