import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, getToken, setToken } from "./api";
import { markSignedOut } from "./platformSso";
import type { User } from "./types";

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<User>;
  logout: () => void;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState<boolean>(!!getToken());

  useEffect(() => {
    if (!getToken()) return;
    api<User>("/auth/me")
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const onExpired = () => setUser(null);
    // Signed in again with the platform session (it may be another platform user now): reload who we are.
    const onRenewed = () => { api<User>("/auth/me").then(setUser).catch(() => undefined); };
    window.addEventListener("auth:expired", onExpired);
    window.addEventListener("auth:renewed", onRenewed);
    return () => {
      window.removeEventListener("auth:expired", onExpired);
      window.removeEventListener("auth:renewed", onRenewed);
    };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const res = await api<{ access_token: string; user: User }>("/auth/login", {
      body: { email, password },
      auth: false,
    });
    setToken(res.access_token);
    setUser(res.user);
    return res.user;
  }, []);

  const logout = useCallback(() => {
    // Opened from WorkflowHub: don't sign straight back in with the same platform login.
    markSignedOut().catch(() => undefined);
    setToken(null);
    setUser(null);
  }, []);

  return <Ctx.Provider value={{ user, loading, login, logout }}>{children}</Ctx.Provider>;
}

export function useAuth() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
