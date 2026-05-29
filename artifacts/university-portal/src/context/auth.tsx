import { createContext, useContext, useEffect, useState, ReactNode, useCallback } from "react";
import { fetchWithAuth, setAuthToken, loadAuthToken } from "@/lib/api";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

interface User {
  id?: number;
  email: string;
  name: string;
  role: string;
}

interface AuthState {
  user: User | null;
  loading: boolean;
  isSuperAdmin: boolean;
  permissions: Set<string>;
  can: (key: string) => boolean;
  canAny: (keys: string[]) => boolean;
  login: (email: string, password: string) => Promise<void>;
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
  login: async () => {},
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

  async function login(email: string, password: string) {
    const res = await fetch(`${BASE}/api/auth/login`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
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
    if (data.token) {
      setAuthToken(data.token);
    }
    setUser(data.user);
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
      value={{ user, loading, isSuperAdmin, permissions, can, canAny, login, logout, refresh }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
