WITH first_seen AS (
  SELECT
    submission_date,
    sample_id,
    client_id,
    first_seen_date,
    second_seen_date
  FROM
    telemetry.clients_last_seen
  WHERE
    days_since_seen = 0
    AND submission_date = @submission_date
)
SELECT
  *
FROM
  telemetry_derived.clients_daily_v6 AS cd
LEFT JOIN
  telemetry_derived.clients_daily_event_v1 AS cde
USING
  (submission_date, sample_id, client_id)
LEFT JOIN
  first_seen
USING
  (submission_date, sample_id, client_id)
WHERE
  cd.submission_date = @submission_date
  AND cde.submission_date = @submission_date
