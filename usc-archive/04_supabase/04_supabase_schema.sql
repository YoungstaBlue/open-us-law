-- Verbatim statutory archive: canonical metadata + full text.
-- Raw source captures (HTML/XML) stay as immutable files; this table holds the validated record.
-- jurisdiction lets a future Missouri RSMo archive share the same table.

create extension if not exists pg_trgm;

create table if not exists statute_sections (
  id                 bigint generated always as identity primary key,
  jurisdiction       text        not null check (jurisdiction in ('federal','missouri')),
  title_number       text        not null,
  title_name         text,
  chapter_number     text,
  chapter_name       text,
  section_number     text        not null,
  catchline          text,
  verbatim_text      text        not null,
  source_credit      text,
  notes_text         text,
  effective_date     text,
  history_text       text,
  status             text        not null default 'operative',
  positive_law_title boolean,
  official_url       text        not null,
  bulk_source_url    text,
  source_edition     text,                  -- e.g. "OLRC USLM Online@119-102" or "govinfo USCODE-2023"
  retrieved_at       timestamptz not null,
  sha256             text        not null,
  source_file_path   text        not null,
  last_verified_at   timestamptz not null,
  is_current         boolean     not null default true,   -- false = superseded version kept for audit
  created_at         timestamptz not null default now(),
  search_vector      tsvector generated always as (
      setweight(to_tsvector('english', coalesce(catchline,'')), 'A') ||
      setweight(to_tsvector('english', coalesce(verbatim_text,'')), 'B') ||
      setweight(to_tsvector('english', coalesce(notes_text,'')), 'C')
  ) stored
);

-- One current row per section; superseded versions coexist with is_current=false.
create unique index if not exists statute_sections_current_uq
  on statute_sections (jurisdiction, title_number, section_number) where is_current;
create index if not exists statute_sections_sha_idx on statute_sections (sha256);
create index if not exists statute_sections_fts_idx on statute_sections using gin (search_vector);
create index if not exists statute_sections_cite_trgm on statute_sections using gin ((title_number || ' USC ' || section_number) gin_trgm_ops);

-- Audit trail for Phase 6 updates.
create table if not exists statute_change_log (
  id               bigint generated always as identity primary key,
  jurisdiction     text not null,
  title_number     text not null,
  section_number   text not null,
  old_sha256       text,
  new_sha256       text not null,
  old_row_id       bigint references statute_sections(id),
  new_row_id       bigint references statute_sections(id),
  detected_at      timestamptz not null default now(),
  change_kind      text not null check (change_kind in ('new','modified','repealed','unchanged')),
  needs_legal_review boolean not null default true,
  reviewed_at      timestamptz,
  reviewer_note    text
);
