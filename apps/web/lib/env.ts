// Environment access with sensible dev defaults (see .env.example).

export const env = {
  apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL || "http://api.dnd.localhost",
  keycloakUrl: process.env.NEXT_PUBLIC_KEYCLOAK_URL || "http://localhost:8080",
  keycloakRealm: process.env.NEXT_PUBLIC_KEYCLOAK_REALM || "dnd",
  keycloakClientId: process.env.NEXT_PUBLIC_KEYCLOAK_CLIENT_ID || "dnd-web",
};
