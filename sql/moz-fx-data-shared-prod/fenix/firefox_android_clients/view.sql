CREATE OR REPLACE VIEW
  `moz-fx-data-shared-prod.fenix.firefox_android_clients`
AS
SELECT
  CASE
    WHEN first_seen_date IS NULL
      AND metadata.min_first_session_ping_run_date IS NULL
      THEN metadata.min_metrics_ping_submission_date
    WHEN first_seen_date IS NULL
      AND metadata.min_first_session_ping_run_date IS NOT NULL
      THEN metadata.min_first_session_ping_run_date
    ELSE first_seen_date
  END AS first_seen_date,
  * EXCEPT (first_seen_date, adjust_network, install_source),
  CASE
    WHEN adjust_network IS NULL
      OR adjust_network = ''
      THEN 'Unknown'
    WHEN adjust_network NOT IN (
        'Organic',
        'Google Organic Search',
        'Untrusted Devices',
        'Product Marketing (Owned media)',
        'Google Ads ACI'
      )
      THEN 'Other'
    ELSE adjust_network
  END AS adjust_network,
  CASE
    WHEN install_source IS NULL
      OR install_source = ''
      THEN 'Unknown'
    WHEN install_source NOT IN ('com.android.vending')
      THEN 'Other'
    ELSE install_source
  END AS install_source,
FROM
  `moz-fx-data-shared-prod.fenix_derived.firefox_android_clients_v1`
