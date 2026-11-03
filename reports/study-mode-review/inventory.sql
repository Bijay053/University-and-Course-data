-- DEVELOPMENT ONLY. Every statement is read-only. No correction SQL is provided.
-- Executed via the development database query service; no production access.
SELECT study_mode, count(*) AS courses
FROM courses GROUP BY study_mode ORDER BY count(*) DESC, study_mode;

SELECT count(*) AS total, count(study_mode) AS nonnull,
       count(*) FILTER (WHERE study_mode IS NOT NULL AND btrim(study_mode) = '') AS empty,
       max(length(study_mode)) AS longest
FROM courses;

SELECT c.id, c.name, u.name AS university, c.course_website, c.study_mode,
       c.study_load, c.delivery_mode, c.course_location, c.status,
       c.approval_status, c.updated_at
FROM courses c JOIN universities u ON u.id = c.university_id
WHERE c.study_mode IS NOT NULL AND c.study_mode NOT IN (
 'On Campus', 'Online', 'Blended', 'Full Time', 'Full-time, Part-time',
 'On Campus, Online', 'Full-time', 'Both', 'Part Time',
 'Online, On Campus', 'Part-time', 'Mixed')
ORDER BY c.id;

SELECT c.id, c.name, u.name AS university, c.course_website, c.study_mode
FROM courses c JOIN universities u ON u.id = c.university_id
WHERE c.study_mode IN ('Both', 'Mixed') ORDER BY c.id;

SELECT study_mode, count(*) AS staged_rows
FROM scraped_courses GROUP BY study_mode ORDER BY count(*) DESC, study_mode;

SELECT s.id, s.course_id, s.scrape_job_id, s.course_name, u.name AS university,
       s.course_website, s.study_mode, s.status, s.created_at
FROM scraped_courses s JOIN universities u ON u.id = s.university_id
WHERE s.study_mode IS NOT NULL AND s.study_mode NOT IN (
 'On Campus', 'Online', 'Blended', 'Full Time', 'Full-time, Part-time',
 'On Campus, Online', 'Full-time', 'Both', 'Part Time',
 'Online, On Campus', 'Part-time', 'Mixed')
ORDER BY s.id;
