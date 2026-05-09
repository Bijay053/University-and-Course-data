import { createContext, useContext, useEffect, useState, ReactNode, useCallback } from "react";

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
      const res = await fetch(`${BASE}/api/auth/me`, { credentials: "include" });
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
    setUser(data.user);
    setPermissions(new Set<string>(Array.isArray(data.permissions) ? data.permissions : []));
    setIsSuperAdmin(Boolean(data.is_super_admin));
  }

  async function logout() {
    await fetch(`${BASE}/api/auth/logout`, { method: "POST", credentials: "include" });
    setUser(null);
    setPermissions(new Set());
    setIsSuperAdmin(false);
  }

  const can = useCallback(
    (key: string) => isSuperAdmin || permissions.has(key),
    [isSuperAdmin, permissions],
  );
  const canAny = useCallback(
    (keys: string[]) => isSuperAdmin || keys.some((k) => permissions.has(k)),
    [isSuperAdmin, permissions],
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
