-- Fake dataset for scripts/pii-score/run.sh.
--
-- Invariant required by score.py: every `value` is unique across the table
-- and is not a substring of any other row's value. score.py locates each
-- seeded value in the rendered export by exact string search, so a
-- duplicate or a substring collision would make that lookup ambiguous.
--
-- expected_entity / category mirror manifests/21-litellm-config.yaml:
--   category=mask/block  -> entity is in the "presidio-mask" guardrail's
--                           pii_entities_config (MASK or BLOCK action)
--   category=audit        -> entity is only in "presidio-audit"
--                           (mode: logging_only — never actually redacted)
--   category=decoy        -> is_pii=0, safe "coding workflow" values that
--                           should never be flagged (Terraform-ish ids, env
--                           var names, git SHAs, UUIDs, semver, generic
--                           column names)

DROP TABLE IF EXISTS pii_samples;
CREATE TABLE pii_samples (
  id              INTEGER PRIMARY KEY,
  category        TEXT NOT NULL,   -- mask | block | audit | decoy
  expected_entity TEXT,            -- Presidio entity type; NULL for decoys
  is_pii          INTEGER NOT NULL,
  value           TEXT NOT NULL UNIQUE
);

-- MASK tier: PERSON, EMAIL_ADDRESS, PHONE_NUMBER (pii_entities_config: MASK)
INSERT INTO pii_samples (category, expected_entity, is_pii, value) VALUES
  ('mask', 'PERSON', 1, 'John Smith'),
  ('mask', 'PERSON', 1, 'Maria Garcia-Lopez'),
  ('mask', 'PERSON', 1, 'Kenji Watanabe'),
  ('mask', 'PERSON', 1, 'Fatima Al-Sayed'),
  ('mask', 'PERSON', 1, 'Liam O''Connor'),
  ('mask', 'EMAIL_ADDRESS', 1, 'j.smith88@northwind-shipping.com'),
  ('mask', 'EMAIL_ADDRESS', 1, 'maria.garcia.lopez@acme-industries.net'),
  ('mask', 'EMAIL_ADDRESS', 1, 'kwatanabe@sakura-tech.jp'),
  ('mask', 'EMAIL_ADDRESS', 1, 'fatima.alsayed@desertrose-consulting.ae'),
  ('mask', 'EMAIL_ADDRESS', 1, 'liam.oconnor@emerald-analytics.ie'),
  ('mask', 'PHONE_NUMBER', 1, '+33 6 12 34 56 78'),
  ('mask', 'PHONE_NUMBER', 1, '+1 415 555 0182'),
  ('mask', 'PHONE_NUMBER', 1, '+81 90 1234 5678'),
  ('mask', 'PHONE_NUMBER', 1, '+971 50 123 4567'),
  ('mask', 'PHONE_NUMBER', 1, '+353 87 654 3210');

-- BLOCK tier: CREDIT_CARD, IBAN_CODE (pii_entities_config: BLOCK)
INSERT INTO pii_samples (category, expected_entity, is_pii, value) VALUES
  ('block', 'CREDIT_CARD', 1, '4539 1488 0343 6467'),
  ('block', 'CREDIT_CARD', 1, '5425 2334 3010 9903'),
  ('block', 'CREDIT_CARD', 1, '3742 454554 00126'),
  ('block', 'IBAN_CODE', 1, 'DE89370400440532013000'),
  ('block', 'IBAN_CODE', 1, 'FR7630006000011234567890189'),
  ('block', 'IBAN_CODE', 1, 'GB29NWBK60161331926819');

-- AUDIT tier: only in the logging_only guardrail — never actually masked in
-- production, so scored as a separate "would this get logged" ratio.
INSERT INTO pii_samples (category, expected_entity, is_pii, value) VALUES
  ('audit', 'LOCATION', 1, 'Paris, France'),
  ('audit', 'LOCATION', 1, '1600 Amphitheatre Parkway, Mountain View'),
  ('audit', 'LOCATION', 1, 'Shibuya, Tokyo'),
  ('audit', 'DATE_TIME', 1, 'March 3rd, 2027'),
  ('audit', 'DATE_TIME', 1, 'next Tuesday at 10am'),
  ('audit', 'DATE_TIME', 1, '14/07/1998');

-- Decoys: safe values styled as "coding workflow" false-positive bait.
-- Deliberately excludes boundary cases (example.com emails, the classic
-- 4111-1111-1111-1111 test card) — those are genuinely ambiguous and are
-- left for the follow-on "safe code context" feature to resolve.
INSERT INTO pii_samples (category, expected_entity, is_pii, value) VALUES
  ('decoy', NULL, 0, 'aws_iam_role.admin'),
  ('decoy', NULL, 0, 'module.vpc.subnet_ids[0]'),
  ('decoy', NULL, 0, 'google_compute_instance.web_server'),
  ('decoy', NULL, 0, 'DATABASE_URL'),
  ('decoy', NULL, 0, 'LITELLM_MASTER_KEY'),
  ('decoy', NULL, 0, 'ANTHROPIC_API_KEY'),
  ('decoy', NULL, 0, 'e5dffe9a3b1c4d2e8f907a6b5c4d3e2f1a0b9c8d'),
  ('decoy', NULL, 0, '65cbae811122334455667788990011223344556a'),
  ('decoy', NULL, 0, '550e8400-e29b-41d4-a716-446655440000'),
  ('decoy', NULL, 0, '6ba7b810-9dad-11d1-80b4-00c04fd430c8'),
  ('decoy', NULL, 0, 'v1.102.1'),
  ('decoy', NULL, 0, '2.4.0-beta.3'),
  ('decoy', NULL, 0, 'customer_id'),
  ('decoy', NULL, 0, 'created_at'),
  ('decoy', NULL, 0, 'shipment_status'),
  ('decoy', NULL, 0, 'presidio-analyzer.internal.svc.cluster.local'),
  ('decoy', NULL, 0, 'us-east-1a'),
  ('decoy', NULL, 0, 'node-pool-default');
