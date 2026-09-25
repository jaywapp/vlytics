DO $migration$
BEGIN
    IF current_user <> 'vlytics_migrator' THEN
        RAISE EXCEPTION 'migrations must run as vlytics_migrator, got %', current_user;
    END IF;
END
$migration$;

SET LOCAL ROLE vlytics_migration_owner;

ALTER TABLE ops.provider_budget_reservations
    ADD COLUMN currency char(3) NOT NULL DEFAULT 'USD',
    ADD CONSTRAINT provider_budget_currency_iso
        CHECK (currency ~ '^[A-Z]{3}$');

ALTER TABLE ops.provider_budget_reservations ALTER COLUMN currency DROP DEFAULT;

COMMENT ON COLUMN ops.provider_budget_reservations.currency IS
    'ISO 4217 budget currency captured with the immutable reservation';

GRANT SELECT ON ops.provider_budget_reservations TO vlytics_read_api;

RESET ROLE;
