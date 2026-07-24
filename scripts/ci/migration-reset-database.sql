\set ON_ERROR_STOP on

-- CI-only isolation between the upgrade and fresh-install scenarios. The shell
-- runner verifies the connected server address is loopback before this executes.
drop extension if exists vector cascade;
drop extension if exists pgcrypto cascade;
drop schema if exists audit_evidence_archive cascade;
drop schema if exists analytics cascade;
drop schema if exists auth cascade;
drop schema if exists public cascade;
create schema public authorization current_user;
grant all on schema public to public;
