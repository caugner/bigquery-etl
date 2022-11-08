CREATE OR REPLACE VIEW
  `moz-fx-data-shared-prod.subscription_platform.stripe_subscriptions_history`
AS
WITH subscriptions_history AS (
  SELECT
    customer_id,
    id AS subscription_id,
    _fivetran_synced AS synced_at,
    _fivetran_start AS valid_from,
    LEAD(_fivetran_start) OVER (PARTITION BY id ORDER BY _fivetran_start) AS valid_to,
    created,
    trial_start,
    trial_end,
    COALESCE(trial_end, start_date) AS subscription_start_date,
    cancel_at,
    cancel_at_period_end,
    canceled_at,
    JSON_VALUE(metadata, "$.cancelled_for_customer_at") AS canceled_for_customer_at,
    ended_at,
    status,
    TIMESTAMP_SECONDS(
      CAST(JSON_VALUE(metadata, "$.plan_change_date") AS INT64)
    ) AS plan_change_date,
    JSON_VALUE(metadata, "$.previous_plan_id") AS previous_plan_id,
  FROM
    `moz-fx-data-bq-fivetran`.stripe.subscription_history
),
subscriptions_history_with_lead_plan_metadata AS (
  SELECT
    *,
    LEAD(plan_change_date) OVER (
      PARTITION BY
        subscription_id
      ORDER BY
        valid_from
    ) AS lead_plan_change_date,
    LEAD(previous_plan_id) OVER (
      PARTITION BY
        subscription_id
      ORDER BY
        valid_from
    ) AS lead_previous_plan_id,
  FROM
    subscriptions_history
),
subscriptions_history_with_plan_ends AS (
  SELECT
    *,
    -- A new `previous_plan_id` value means the previous row was the last row with that plan.
    IF(
      lead_previous_plan_id IS DISTINCT FROM previous_plan_id,
      STRUCT(lead_previous_plan_id AS plan_id, lead_plan_change_date AS plan_ended_at),
      (NULL, NULL)
    ).*
  FROM
    subscriptions_history_with_lead_plan_metadata
),
subscriptions_history_with_previous_plan_ids AS (
  -- Fill in previous `plan_id` values by looking forward to the next non-null `plan_id`.
  SELECT
    * EXCEPT (plan_id),
    FIRST_VALUE(plan_id IGNORE NULLS) OVER (
      PARTITION BY
        subscription_id
      ORDER BY
        valid_from
      ROWS BETWEEN
        CURRENT ROW
        AND UNBOUNDED FOLLOWING
    ) AS plan_id,
    LAST_VALUE(plan_ended_at IGNORE NULLS) OVER (
      PARTITION BY
        subscription_id
      ORDER BY
        valid_from
      ROWS BETWEEN
        UNBOUNDED PRECEDING
        AND 1 PRECEDING
    ) AS plan_started_at,
  FROM
    subscriptions_history_with_plan_ends
),
subscription_items AS (
  SELECT
    id AS subscription_item_id,
    subscription_id,
    plan_id,
  FROM
    `moz-fx-data-bq-fivetran`.stripe.subscription_item
  -- ZetaSQL requires QUALIFY to be used in conjunction with WHERE, GROUP BY, or HAVING.
  WHERE
    TRUE
  QUALIFY
    -- With how our subscription platform currently works each Stripe subscription should
    -- only have one subscription item, and we enforce that so the ETL can rely on it.
    1 = COUNT(*) OVER (PARTITION BY subscription_id)
),
subscriptions_history_with_plan_ids AS (
  -- Fill in current `plan_id` values from subscription items.
  SELECT
    subscriptions_history.* REPLACE (
      COALESCE(subscriptions_history.plan_id, subscription_items.plan_id) AS plan_id
    ),
    subscription_items.subscription_item_id,
  FROM
    subscriptions_history_with_previous_plan_ids AS subscriptions_history
  JOIN
    subscription_items
  USING
    (subscription_id)
),
plans AS (
  SELECT
    plans.id AS plan_id,
    plans.nickname AS plan_name,
    plans.amount AS plan_amount,
    plans.billing_scheme AS billing_scheme,
    plans.currency AS plan_currency,
    plans.interval AS plan_interval,
    plans.interval_count AS plan_interval_count,
    plans.product_id,
    products.name AS product_name,
  FROM
    `moz-fx-data-bq-fivetran`.stripe.plan AS plans
  LEFT JOIN
    `moz-fx-data-bq-fivetran`.stripe.product AS products
  ON
    plans.product_id = products.id
),
customers AS (
  SELECT
    id AS customer_id,
    COALESCE(
      TO_HEX(SHA256(JSON_VALUE(customers.metadata, "$.userid"))),
      JSON_VALUE(pre_fivetran_customers.metadata, "$.fxa_uid")
    ) AS fxa_uid,
    COALESCE(customers.address_country, pre_fivetran_customers.address_country) AS address_country,
  FROM
    `moz-fx-data-bq-fivetran`.stripe.customer AS customers
  FULL JOIN
    -- Include customers that were deleted before the initial Fivetran Stripe import.
    `moz-fx-data-shared-prod`.stripe_external.pre_fivetran_customers
  USING
    (id)
),
charges AS (
  SELECT
    charges.id AS charge_id,
    COALESCE(cards.country, charges.billing_detail_address_country) AS country,
  FROM
    `moz-fx-data-bq-fivetran`.stripe.charge AS charges
  JOIN
    `moz-fx-data-bq-fivetran`.stripe.card AS cards
  ON
    charges.card_id = cards.id
  WHERE
    charges.status = "succeeded"
),
invoices_provider_country AS (
  SELECT
    invoices.subscription_id,
    IF(
      JSON_VALUE(invoices.metadata, "$.paypalTransactionId") IS NOT NULL,
      -- FxA copies PayPal billing agreement country to customer address.
      STRUCT("Paypal" AS provider, customers.address_country AS country),
      ("Stripe", charges.country)
    ).*,
    invoices.created,
  FROM
    `moz-fx-data-bq-fivetran`.stripe.invoice AS invoices
  LEFT JOIN
    customers
  USING
    (customer_id)
  LEFT JOIN
    charges
  USING
    (charge_id)
  WHERE
    invoices.status = "paid"
),
subscriptions_history_provider_country AS (
  SELECT
    subscriptions_history.subscription_id,
    subscriptions_history.valid_from,
    ARRAY_AGG(
      STRUCT(
        invoices_provider_country.provider,
        LOWER(invoices_provider_country.country) AS country
      )
      ORDER BY
        -- prefer rows with country
        IF(invoices_provider_country.country IS NULL, 0, 1) DESC,
        invoices_provider_country.created DESC
      LIMIT
        1
    )[OFFSET(0)].*
  FROM
    subscriptions_history
  JOIN
    invoices_provider_country
  ON
    subscriptions_history.subscription_id = invoices_provider_country.subscription_id
    AND (
      invoices_provider_country.created < subscriptions_history.valid_to
      OR subscriptions_history.valid_to IS NULL
    )
  GROUP BY
    subscription_id,
    valid_from
),
subscriptions_history_promotions AS (
  SELECT
    subscriptions_history.subscription_id,
    subscriptions_history.valid_from,
    ARRAY_AGG(DISTINCT promotion_codes.code IGNORE NULLS) AS promotion_codes,
    SUM(
      COALESCE(coupons.amount_off, 0) + COALESCE(
        CAST((invoices.subtotal * coupons.percent_off / 100) AS INT64),
        0
      )
    ) AS promotion_discounts_amount,
  FROM
    subscriptions_history
  JOIN
    `moz-fx-data-bq-fivetran`.stripe.invoice AS invoices
  ON
    subscriptions_history.subscription_id = invoices.subscription_id
    AND (
      invoices.created < subscriptions_history.valid_to
      OR subscriptions_history.valid_to IS NULL
    )
  JOIN
    `moz-fx-data-bq-fivetran`.stripe.invoice_discount AS invoice_discounts
  ON
    invoices.id = invoice_discounts.invoice_id
  JOIN
    `moz-fx-data-bq-fivetran`.stripe.promotion_code AS promotion_codes
  ON
    invoice_discounts.promotion_code = promotion_codes.id
  JOIN
    `moz-fx-data-bq-fivetran`.stripe.coupon AS coupons
  ON
    promotion_codes.coupon_id = coupons.id
  WHERE
    invoices.status = "paid"
  GROUP BY
    subscription_id,
    valid_from
)
SELECT
  subscriptions_history.customer_id,
  customers.fxa_uid,
  subscriptions_history.subscription_id,
  subscriptions_history.subscription_item_id,
  subscriptions_history.synced_at,
  subscriptions_history.valid_from,
  subscriptions_history.valid_to,
  subscriptions_history.created,
  subscriptions_history.trial_start,
  subscriptions_history.trial_end,
  subscriptions_history.subscription_start_date,
  subscriptions_history.cancel_at,
  subscriptions_history.cancel_at_period_end,
  subscriptions_history.canceled_at,
  subscriptions_history.canceled_for_customer_at,
  subscriptions_history.ended_at,
  subscriptions_history.status,
  plans.product_id,
  plans.product_name,
  subscriptions_history.plan_id,
  subscriptions_history.plan_started_at,
  subscriptions_history.plan_ended_at,
  plans.plan_name,
  plans.plan_amount,
  plans.billing_scheme,
  plans.plan_currency,
  plans.plan_interval,
  plans.plan_interval_count,
  "Etc/UTC" AS plan_interval_timezone,
  subscriptions_history_provider_country.provider,
  subscriptions_history_provider_country.country,
  subscriptions_history_promotions.promotion_codes,
  subscriptions_history_promotions.promotion_discounts_amount,
FROM
  subscriptions_history_with_plan_ids AS subscriptions_history
LEFT JOIN
  plans
USING
  (plan_id)
LEFT JOIN
  customers
USING
  (customer_id)
LEFT JOIN
  subscriptions_history_provider_country
USING
  (subscription_id, valid_from)
LEFT JOIN
  subscriptions_history_promotions
USING
  (subscription_id, valid_from)
