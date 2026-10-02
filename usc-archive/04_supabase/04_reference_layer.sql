-- Reference layer: correct citations for every corpus, jurisdiction filter, generic exact lookup.
-- Fixes: search_statutes() used to label every row "<title> U.S.C. § <section>", Missouri included.

alter table statute_sections add column if not exists citation text;

update statute_sections set citation = '§ ' || section_number || ', RSMo'
  where jurisdiction = 'missouri' and citation is null;
update statute_sections set citation = title_number || ' U.S.C. § ' || section_number
  where jurisdiction = 'federal' and section_id like 'US-USC-%' and citation is null;

drop function if exists search_statutes(text, int);
create or replace function search_statutes(q text, lim int default 10, juris text default null)
returns table (
  section_id text, citation text, catchline text, chapter text, status text, verbatim_text text,
  source_credit text, notes_text text, official_url text, source_edition text,
  retrieved_at timestamptz, sha256 text, rank real
)
language sql stable as $$
  select s.section_id, s.citation, s.catchline,
    coalesce(s.chapter_number || coalesce(' — ' || s.chapter_name, ''), ''),
    s.status, s.verbatim_text, s.source_credit, s.notes_text, s.official_url,
    s.source_edition, s.retrieved_at, s.sha256,
    greatest(ts_rank(s.search_vector, websearch_to_tsquery('english', q)),
             similarity(coalesce(s.citation, ''), q))::real as rank
  from statute_sections s
  where s.is_current
    and (juris is null or s.jurisdiction = juris)
    and (s.search_vector @@ websearch_to_tsquery('english', q)
         or coalesce(s.citation, '') % q
         or s.section_number = q)
  order by rank desc, s.title_number, s.section_number
  limit lim
$$;

-- Exact lookup: get_statute('missouri', '565.056') or get_statute('federal', '1983', '42').
drop function if exists get_statute(text, text);
create or replace function get_statute(juris text, section text, title text default null)
returns setof statute_sections
language sql stable as $$
  select * from statute_sections
  where jurisdiction = juris and section_number = section and (title is null or title_number = title)
  order by is_current desc, retrieved_at desc
$$;

grant execute on function search_statutes(text, int, text) to anon, authenticated;
grant execute on function get_statute(text, text, text) to anon, authenticated;
