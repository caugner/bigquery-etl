WITH host_stripped AS (
  SELECT
    SPLIT(pge.metrics.url2.page_path, 'https://developer.mozilla.org')[OFFSET(1)] AS `path`,
    COUNT(*) AS page_hits,
  FROM
    mdn_yari.page AS pge
  WHERE
    DATE(submission_timestamp)
    BETWEEN DATE_TRUNC(@submission_date, MONTH)
    AND LAST_DAY(@submission_date)
    AND CONTAINS_SUBSTR(pge.metrics.url2.page_path, 'https://developer.mozilla.org/')
    AND client_info.app_channel = 'prod'
  GROUP BY
    `path`
  ORDER BY
    page_hits DESC
  LIMIT
    100
)
SELECT
  host_stripped.`path`,
  host_stripped.page_hits / FIRST_VALUE(host_stripped.page_hits) OVER (
    ORDER BY
      page_hits DESC
  ) AS popularity
FROM
  host_stripped
WHERE
  ARRAY_LENGTH(SPLIT(host_stripped.`path`, '/')) > 3
