\getenv migrator_password MIGRATOR_DATABASE_PASSWORD

SELECT format(
    'CREATE ROLE vlytics_migrator LOGIN SUPERUSER CREATEDB CREATEROLE INHERIT NOREPLICATION PASSWORD %L',
    :'migrator_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'vlytics_migrator'
)
\gexec

ALTER ROLE vlytics_bootstrap_admin NOLOGIN;
