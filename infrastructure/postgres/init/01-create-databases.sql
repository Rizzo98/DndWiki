-- DnD Wiki — creates one database per service (database-per-service pattern).
-- Runs automatically on the first initialization of the Postgres volume.
CREATE DATABASE dnd_users;
CREATE DATABASE dnd_campaigns;
CREATE DATABASE dnd_sessions;
CREATE DATABASE dnd_wiki;
CREATE DATABASE dnd_content;
CREATE DATABASE dnd_keycloak;
