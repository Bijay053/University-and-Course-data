import { createContext, useContext, useEffect, useState, ReactNode, useCallback } from "react";
import { fetchWithAuth, setAuthToken, loadAuthToken, setOnUnauthorized } from "@/lib/api";
import { setUnauthorizedHandler } from "@workspace/api-client-react";
import { toast } from "@/hooks/use-toast";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

interface User {
  id?: number;
  email: string;
  name: string;
  role: string;
}

export interface LoginChallenge {
  challenge_token: string;
  masked_email: string;
  expires_in: number;
  resend_after: number;
}

interface AuthState {
  user: User | null;
  loading: boolean;
  isSuperAdmin: boolean;
  permissions: Set<string>;
  can: (key: string) => boolean;
  canAny: (keys: string[]) => boolean;
  login: (email: string, password: string) => Promise<LoginChallenge>;
  verifyOtp: (challengeToken: string, code: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState>({
  user: null,
  loading: true,
  isSuperAdmin: false,
  permissions: new Set(),
  can: () => false,
  canAny: () => false,
  login: async () => { throw new Error("Authentication is unavailable"); },
  verifyOtp: async () => {},
  logout: async () => {},
  refresh: async () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [permissions, setPermissions] = useState<Set<string>>(new Set());
  const [isSuperAdmin, setIsSuperAdmin] = useState(false);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      loadAuthToken();
      const res = await fetchWithAuth(`${BASE}/api/auth/me`);
      const data = await res.json();
      setUser(data.user ?? null);
      setPermissions(new Set<string>(Array.isArray(data.permissions) ? data.permissions : []));
      setIsSuperAdmin(Boolean(data.is_super_admin));
    } catch {
      setUser(null);
      setPermissions(new Set());
      setIsSuperAdmin(false);
    }
  }, []);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, [refresh]);

  const forceLogout = useCallback(() => {
    setAuthToken(null);
    setUser((prev) => {
      if (prev) {
        toast({
          title: "Session expired",
          description: "Please log in again.",
          variant: "destructive",
        });
      }
      return null;
    });
    setPermissions(new Set());
    setIsSuperAdmin(false);
  }, []);

  useEffect(() => {
    setOnUnauthorized(forceLogout);
    setUnauthorizedHandler(forceLogout);
    return () => {
      setOnUnauthorized(null);
      setUnauthorizedHandler(null);
    };
  }, [forceLogout]);

  async function authRequest(path: string, body: Record<string, string>) {
    const res = await fetch(`${BASE}/api/auth/${path}`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data: Record<string, unknown> = {};
    try {
      data = await res.json();
    } catch {
      throw new Error(
        res.status === 0
          ? "Cannot reach the server. Please try again."
          : `Server error (${res.status || "no response"}). Please try again.`,
      );
    }
    if (!res.ok) {
      const detail = data?.detail;
      const message =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
          ? detail.map((e: { msg?: string }) => e.msg ?? "Invalid field").join("; ")
          : "Login failed";
      throw new Error(message);
    }
    return data;
  }

  async function login(email: string, password: string): Promise<LoginChallenge> {
    const data = await authRequest("login", { email, password });
    if (data.otp_required !== true || typeof data.challenge_token !== "string") {
      throw new Error("Email verification is required. Please reload and try again.");
    }
    // A challenge is not an authenticated session and stays only in component memory.
    setAuthToken(null);
    setUser(null);
    setPermissions(new Set());
    setIsSuperAdmin(false);
    return {
      challenge_token: data.challenge_token,
      masked_email: String(data.masked_email ?? "your account email"),
      expires_in: Number(data.expires_in ?? 600),
      resend_after: Number(data.resend_after ?? 60),
    };
  }

  async function verifyOtp(challengeToken: string, code: string) {
    const data = await authRequest("verify-otp", { challenge_token: challengeToken, code });
    if (typeof data.token !== "string" || !data.user) {
      throw new Error("Verification did not complete. Please try again.");
    }
    setAuthToken(data.token);
    setUser((data.user as User) ?? null);
    setPermissions(new Set<string>(Array.isArray(data.permissions) ? data.permissions : []));
    setIsSuperAdmin(Boolean(data.is_super_admin));
  }

  async function logout() {
    await fetchWithAuth(`${BASE}/api/auth/logout`, { method: "POST" });
    setAuthToken(null);
    setUser(null);
    setPermissions(new Set());
    setIsSuperAdmin(false);
  }

  // Super-admins always pass; also check user.role === "admin" as a
  // belt-and-suspenders fallback in case isSuperAdmin state hasn't been
  // hydrated yet (e.g. stale JWT cookie with is_super_admin=false while
  // the /me response is in-flight and the DB fix hasn't propagated).
  const isAdmin = isSuperAdmin || user?.role === "admin";

  const can = useCallback(
    (key: string) => isAdmin || permissions.has(key),
    [isAdmin, permissions],
  );
  const canAny = useCallback(
    (keys: string[]) => isAdmin || keys.some((k) => permissions.has(k)),
    [isAdmin, permissions],
  );

  return (
    <AuthContext.Provider
      value={{ user, loading, isSuperAdmin, permissions, can, canAny, login, verifyOtp, logout, refresh }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
