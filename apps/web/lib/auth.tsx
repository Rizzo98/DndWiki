"use client";

import Keycloak, { type KeycloakTokenParsed } from "keycloak-js";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { env } from "./env";

export interface AuthState {
  initialized: boolean;
  authenticated: boolean;
  token: string | null;
  tokenParsed: KeycloakTokenParsed | null;
  realmRoles: string[];
  isDm: boolean;
  userId: string | null;
  /** Users with the 'dev' realm role may drive the summary review of any campaign. */
  isDeveloper: boolean;
  login: (redirectUri?: string) => void;
  logout: () => void;
  refresh: () => Promise<boolean>;
}

/** Keycloak realm role that unlocks the debug tools. Assign it in Keycloak. */
export const DEV_ROLE = "dev";

const AuthContext = createContext<AuthState | null>(null);

let _kc: Keycloak | null = null;
function getKeycloak(): Keycloak {
  if (!_kc) {
    _kc = new Keycloak({
      url: env.keycloakUrl,
      realm: env.keycloakRealm,
      clientId: env.keycloakClientId,
    });
  }
  return _kc;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [initialized, setInitialized] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const [tokenParsed, setTokenParsed] = useState<KeycloakTokenParsed | null>(null);
  const intervalRef = useRef<number | null>(null);

  useEffect(() => {
    const kc = getKeycloak();
    let disposed = false;

    const applyAuth = (auth: boolean) => {
      setAuthenticated(auth);
      setToken(auth ? (kc.token ?? null) : null);
      setTokenParsed(auth ? (kc.tokenParsed ?? null) : null);
      return auth;
    };

    (async () => {
      try {
        const auth = await kc.init({
          onLoad: "check-sso",
          pkceMethod: "S256",
          checkLoginIframe: false,
          silentCheckSsoRedirectUri: `${window.location.origin}/silent-check-sso.html`,
          flow: "standard",
        });
        if (disposed) return;
        applyAuth(auth);
        if (auth) {
          // Refresh the access token before it expires (30 s cadence, 60 s margin).
          intervalRef.current = window.setInterval(async () => {
            try {
              const refreshed = await kc.updateToken(60);
              if (refreshed || !token) {
                setToken(kc.token ?? null);
                setTokenParsed(kc.tokenParsed ?? null);
              }
            } catch {
              if (!disposed) {
                setAuthenticated(false);
                setToken(null);
              }
            }
          }, 30_000);
        }
      } catch (err) {
        console.error("Keycloak init failed", err);
      } finally {
        if (!disposed) setInitialized(true);
      }
    })();

    return () => {
      disposed = true;
      if (intervalRef.current !== null) window.clearInterval(intervalRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = useCallback((redirectUri?: string) => {
    // Guard against callers passing a non-string (e.g. a React MouseEvent from
    // onClick={login}); a non-string would be serialized as "[object Object]"
    // and Keycloak would reject the request with "Invalid parameter: redirect_uri".
    const uri = typeof redirectUri === "string" ? redirectUri : window.location.href;
    getKeycloak().login({ redirectUri: uri });
  }, []);

  const logout = useCallback(() => {
    const kc = getKeycloak();
    setAuthenticated(false);
    setToken(null);
    kc.logout({ redirectUri: window.location.origin + "/" });
  }, []);

  const refresh = useCallback(async () => {
    try {
      return await getKeycloak().updateToken(60);
    } catch {
      return false;
    }
  }, []);

  const value = useMemo<AuthState>(() => {
    const realmRoles = Array.isArray(
      (tokenParsed as { realm_access?: { roles?: unknown } } | null)?.realm_access?.roles,
    )
      ? ((tokenParsed as { realm_access: { roles: string[] } }).realm_access.roles)
      : [];
    const userId =
      tokenParsed && typeof tokenParsed.sub === "string" ? tokenParsed.sub : null;
    return {
      initialized,
      authenticated,
      token,
      tokenParsed,
      realmRoles,
      isDm: realmRoles.includes("dm"),
      userId,
      isDeveloper: realmRoles.includes(DEV_ROLE),
      login,
      logout,
      refresh,
    };
  }, [initialized, authenticated, token, tokenParsed, login, logout, refresh]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

export function SignInPrompt({ onLogin }: { onLogin: () => void }) {
  return (
    <div className="flex min-h-[60vh] items-center justify-center px-4">
      {/* Dark chrome: the app's front door sits on the warm-black frame. */}
      <div className="rl-dark-panel w-full max-w-md p-8 text-center">
        <div className="text-5xl">🎲</div>
        <h2 className="rl-title mt-4 text-2xl text-[color:var(--rl-text-on-dark-primary)]">Welcome to DnD Wiki</h2>
        <p className="rl-dark-note mt-2">
          Sign in with your campaign account to browse the wiki, review drafts and
          manage your sessions.
        </p>
        <button onClick={() => onLogin()} className="rl-btn rl-btn--primary rl-btn--block mt-6">
          Sign in with Keycloak
        </button>
      </div>
    </div>
  );
}

export function LoadingScreen() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center text-[color:var(--rl-text-on-parchment-muted)]">
      <div className="animate-pulse text-sm">Loading…</div>
    </div>
  );
}

/** Wraps a protected page: shows the sign-in prompt until authenticated. */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const { initialized, authenticated, login } = useAuth();
  if (!initialized) return <LoadingScreen />;
  if (!authenticated) return <SignInPrompt onLogin={login} />;
  return <>{children}</>;
}
