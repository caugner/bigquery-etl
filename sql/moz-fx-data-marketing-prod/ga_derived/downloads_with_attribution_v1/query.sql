-- Query for ga_derived.downloads_with_attribution_v1
            -- For more information on writing queries see:
            -- https://docs.telemetry.mozilla.org/cookbooks/bigquery/querying.html
CREATE TEMP FUNCTION normalize_browser(browser STRING) AS (
  CASE
    WHEN `moz-fx-data-shared-prod.udf.ga_is_mozilla_browser`(browser)
      THEN 'Firefox'
    WHEN browser IN ('Internet Explorer')
      THEN 'MSIE'
    WHEN browser IN ('Edge')
      THEN 'Edge'
    WHEN browser IN ('Chrome')
      THEN 'Chrome'
    WHEN browser IN ('Safari')
      THEN 'Safari'
    WHEN browser IN ('(not set)')
      THEN NULL
    WHEN browser IS NULL
      THEN NULL
    ELSE 'Other'
  END
);

WITH all_hits AS (
  SELECT
    clientId AS client_id,
    visitId AS visit_id,
    hit.appInfo.landingScreenName AS landing_page,
    hit.page.pagePath AS pagepath,
    CASE
      WHEN (
          hit.type = 'EVENT'
          AND hit.eventInfo.eventAction = 'Firefox Download'
          AND hit.eventInfo.eventCategory IS NOT NULL
          AND hit.eventInfo.eventLabel LIKE 'Firefox for Desktop%'
        )
        THEN TRUE
      ELSE FALSE
    END AS has_ga_download_event,
    hit.type AS hit_type
  FROM
    `moz-fx-data-marketing-prod.65789850.ga_sessions_*` AS ga
  CROSS JOIN
    UNNEST(hits) AS hit
  WHERE
    _TABLE_SUFFIX = FORMAT_DATE('%Y%m%d', @submission_date)
),

pageviews AS (
  SELECT
    client_id AS client_id,
    visit_id AS visit_id,
    COUNT(pagePath) AS pageviews,
    COUNT(DISTINCT pagePath) AS unique_pageviews,
  FROM
    all_hits
  WHERE
    hit_type = 'PAGE'
  GROUP BY
    client_id,
    visit_id
),

dl_events AS (
  SELECT
    client_id AS client_id,
    visit_id AS visit_id,
    ANY_VALUE(landing_page) AS landing_page,
    LOGICAL_OR(has_ga_download_event) AS has_ga_download_event
  FROM
    all_hits
  GROUP BY
    client_id,
    visit_id
),

ga_sessions AS (
  SELECT
    clientId AS client_id,
    visitId AS visit_id,
    ANY_VALUE(geoNetwork.country) AS country,
    ANY_VALUE(trafficSource.adContent) AS ad_content,
    ANY_VALUE(trafficSource.campaign) AS campaign,
    ANY_VALUE(trafficSource.medium) AS medium,
    ANY_VALUE(trafficSource.source) AS source,
    ANY_VALUE(device.deviceCategory) AS device_category,
    ANY_VALUE(device.operatingSystem) AS os,
    ANY_VALUE(device.browser) AS browser,
    ANY_VALUE(device.browserVersion) AS browser_version,
    ANY_VALUE(device.language) AS `language`
  FROM
    `moz-fx-data-marketing-prod.65789850.ga_sessions_*` AS ga
  WHERE
    _TABLE_SUFFIX = FORMAT_DATE('%Y%m%d', @submission_date)
  GROUP BY
    client_id,
    visit_id
),

ga_sessions_with_hits AS (
  SELECT
    *
  FROM
    ga_sessions ga
  JOIN
    pageviews p
  USING
    (client_id, visit_id)
  JOIN
    dl_events
  USING
    (client_id, visit_id)
),

stub AS (
  SELECT
    s.jsonPayload.fields.visit_id AS stub_visit_id,
    s.jsonPayload.fields.dltoken AS dltoken,
    (COUNT(*) - 1) AS count_dltoken_duplicates,
    DATE(s.timestamp) AS download_date
  FROM
    `moz-fx-stubattribut-prod-32a5.stubattribution_prod.stdout` s
  WHERE
    DATE(s.timestamp) = @submission_date
    AND s.jsonPayload.fields.log_type = 'download_started'
  GROUP BY
    stub_visit_id,
    dltoken,
    DATE(s.timestamp)
),
-- This will drop all the ga_sessions w/o a DLtoken but keep DLtoken without a GA session.
-- This will also result in multiple rows is the ga.client_id is not unique for the day
-- since this visit_id is missing from the stub.
downloads_and_ga_session AS (
  SELECT
    gs.client_id AS client_id,
    ANY_VALUE(country) AS country,
    ANY_VALUE(ad_content) AS ad_content,
    ANY_VALUE(campaign) AS campaign,
    ANY_VALUE(medium) AS medium,
    ANY_VALUE(source) AS source,
    ANY_VALUE(device_category) AS device_category,
    ANY_VALUE(os) AS os,
    ANY_VALUE(browser) AS browser,
    ANY_VALUE(browser_version) AS browser_version,
    ANY_VALUE(`language`) AS `language`,
    s.stub_visit_id AS stub_visit_id,
    s.dltoken AS dltoken,
    ANY_VALUE(landing_page) AS landing_page,
    ANY_VALUE(pageviews) AS pageviews,
    ANY_VALUE(unique_pageviews) AS unique_pageviews,
    LOGICAL_OR(
      has_ga_download_event
    ) AS has_ga_download_event,  -- this will be ignored if nrows >1
    ANY_VALUE(count_dltoken_duplicates) AS count_dltoken_duplicates,
    COUNT(*) AS nrows,
    ANY_VALUE(s.download_date) AS download_date
  FROM
    ga_sessions_with_hits gs
  RIGHT JOIN
    stub s
  ON
    gs.client_id = s.stub_visit_id
  GROUP BY
    gs.client_id,
    dltoken,
    stub_visit_id
)
SELECT
  dltoken,
  download_date,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE ad_content
  END
  ad_content,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE campaign
  END
  campaign,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE medium
  END
  medium,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE source
  END
  source,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE landing_page
  END
  landing_page,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE country
  END
  country,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE cn.code
  END
  normalized_country_code,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE device_category
  END
  device_category,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE os
  END
  os,
  CASE
    WHEN nrows > 1
      THEN NULL
    WHEN os IS NULL
      THEN NULL
    WHEN os LIKE 'Macintosh%'
      THEN 'Mac'  -- these values are coming from GA.
    ELSE mozfun.norm.os(os)
  END
  normalized_os,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE browser
  END
  browser,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE normalize_browser(browser)
  END
  normalized_browser,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE browser_version
  END
  browser_version,
 -- only setting browser major version since that is the only value used in
 -- moz-fx-data-shared-prod.firefox_installer.install
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE CAST(mozfun.norm.extract_version(browser_version, 'major') AS INTEGER)
  END
  browser_major_version,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE `language`
  END
  `language`,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE pageviews
  END
  pageviews,
  CASE
    WHEN nrows > 1
      THEN NULL
    ELSE unique_pageviews
  END
  unique_pageviews,
  has_ga_download_event,
  count_dltoken_duplicates,
  CASE
    WHEN stub_visit_id = ''
      THEN 'DOWNLOAD_SESSION_ID_EMPTY'
    WHEN stub_visit_id = '(not set)'
      THEN 'DOWNLOAD_SESSION_ID_VALUE_NOT_SET'
    WHEN stub_visit_id = 'something'
      THEN 'DOWNLOAD_SESSION_ID_VALUE_SOMETHING'
    WHEN client_id IS NULL
      THEN 'MISSING_GA_CLIENT'
    WHEN dltoken IS NULL
      THEN 'MISSING_DL_TOKEN'
    WHEN nrows > 1
      THEN 'GA_UNRESOLVABLE'
    ELSE NULL
  END
  `exception`
FROM
  downloads_and_ga_session
LEFT JOIN
  `moz-fx-data-shared-prod.static.country_names_v1` AS cn
ON
  cn.name = country
