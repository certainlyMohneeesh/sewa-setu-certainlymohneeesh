-- Database forensics quoted in FORENSICS.md. Read-only.
--   psql -h <host> -U sewasetu -d sewasetu -f forensics/db_checks.sql
-- "Reference date" for overdue counts: change :ref if you re-run on another day.
\set ref '''2026-10-15 04:34'''

\echo '== status x decided_by'
SELECT status, coalesce(decided_by, '<NULL>') AS decided_by, count(*)
FROM applications GROUP BY 1, 2 ORDER BY 1, 2;

\echo '== PENDING rows past the 15-day RTPS window'
SELECT count(*) AS pending,
       count(*) FILTER (WHERE submitted_at <  :ref::timestamp - interval '15 days') AS overdue,
       count(*) FILTER (WHERE submitted_at >= :ref::timestamp - interval '15 days') AS within_sla,
       min(submitted_at) AS oldest, max(submitted_at) AS newest
FROM applications WHERE status = 'PENDING';

\echo '== overdue PENDING by submission month'
SELECT to_char(submitted_at, 'YYYY-MM') AS month, count(*)
FROM applications
WHERE status = 'PENDING' AND submitted_at < :ref::timestamp - interval '15 days'
GROUP BY 1 ORDER BY 1;

\echo '== last DEEMED_APPROVED decision'
SELECT min(decided_at), max(decided_at), count(*) FROM applications WHERE status = 'DEEMED_APPROVED';

\echo '== names: non-ASCII, mojibake (UTF-8 read as Latin-1), 20-char ceiling'
SELECT count(*) FILTER (WHERE applicant_name ~ '[^\x01-\x7f]')                          AS non_ascii,
       count(*) FILTER (WHERE applicant_name ~ '[^\x01-\x7f]' AND length(applicant_name) <= 7) AS truncated_bengali_assamese,
       count(*) FILTER (WHERE applicant_name ~ '[^\x01-\x7f]' AND length(applicant_name) >= 18) AS mojibake,
       count(*) FILTER (WHERE applicant_name LIKE '%?%')                                AS literal_question_marks,
       count(*) FILTER (WHERE length(applicant_name) = 20)                               AS exactly_20_chars,
       max(length(applicant_name))                                                       AS max_chars,
       max(octet_length(applicant_name))                                                 AS max_bytes
FROM applications;

\echo '== portal_users integrity'
SELECT count(*) FILTER (WHERE p.mobile IS NULL) AS applications_without_login,
       count(*) FILTER (WHERE p.password_hash <> encode(sha256(convert_to(to_char(a.dob, 'DDMMYYYY'), 'UTF8')), 'hex')) AS hash_not_ddmmyyyy_of_dob
FROM applications a LEFT JOIN portal_users p ON p.mobile = a.mobile;
SELECT count(*) AS logins_without_application
FROM portal_users p LEFT JOIN applications a ON a.mobile = p.mobile WHERE a.mobile IS NULL;

\echo '== other facts'
SELECT count(*) AS rows, count(DISTINCT mobile) AS distinct_mobiles,
       count(*) FILTER (WHERE doc_path <> '') AS with_document,
       count(*) FILTER (WHERE age(submitted_at::date, dob) < interval '60 years') AS under_60_at_submission
FROM applications;
SELECT count(*) AS widowed, count(*) FILTER (WHERE coalesce(husband_employer, '') = '') AS widowed_blank_employer
FROM applications WHERE marital_status = 'Widowed';
SELECT schemaname, tablename, indexname FROM pg_indexes WHERE tablename IN ('applications', 'otps', 'portal_users');
