-- DnD Wiki — creates one database per service (database-per-service pattern).
-- Runs automatically on the first initialization of the Postgres volume.
CREATE DATABASE dnd_users;
CREATE DATABASE dnd_campaigns;
CREATE DATABASE dnd_sessions;
CREATE DATABASE dnd_wiki;
CREATE DATABASE dnd_content;
-- attribution-service: the evidence-first speaker attribution engine
-- (docs/attribution-model.md). Only used when ATTRIBUTION_ENABLED=true.
CREATE DATABASE dnd_attribution;
CREATE DATABASE dnd_keycloak;
