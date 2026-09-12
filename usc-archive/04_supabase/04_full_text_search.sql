-- Full-text search over the verbatim archive.
-- Returns the full statute, its official link, retrieval date, and hash — never a summary.
--
--   select * from search_statutes('under color of');
--   select * from search_statutes('pattern or practice', 5);
--   select * from search_statutes('1983');            -- citation lookup also works (trigram)

create or replace function search_statutes(q text, lim int default 10)
returns table (
  section_id       text,
  citation         text,
  catchline        text,
  chapter          text,
  status           text,
  verbatim_text    text,
  source_credit    text,
  notes_text       text,
  official_url     text,
  source_edition   text,
  retrieved_at     timestamptz,
  sha256           text,
  rank             real
)
language sql stable as $$
  select
    s.section_id,
    s.title_number || ' U.S.C. § ' || s.section_number,
    s.catchline,
    coalesce(s.chapter_number || ' — ' || s.chapter_name, ''),
    s.status,
    s.verbatim_text,
    s.source_credit,
    s.notes_text,
    s.official_url,
    s.source_edition,
    s.retrieved_at,
    s.sha256,
    greatest(
      ts_rank(s.search_vector, websearch_to_tsquery('english', q)),
      similarity(s.title_number || ' USC ' || s.section_number, q)
    )::real as rank
  from statute_sections s
  where s.is_current
    and (
      s.search_vector @@ websearch_to_tsquery('english', q)
      or (s.title_number || ' USC ' || s.section_number) % q
      or s.section_number = q
    )
  order by rank desc, s.title_number, s.section_number
  limit lim
$$;

-- Exact citation fetch (no ranking).
create or replace function get_statute(title text, section text)
returns setof statute_sections
language sql stable as $$
  select * from statute_sections
  where jurisdiction = 'federal' and title_number = title and section_number = section
  order by is_current desc, retrieved_at desc
$$;

grant execute on function search_statutes(text, int) to anon, authenticated;
grant execute on function get_statute(text, text) to anon, authenticated;
