"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { api, ApiError, getActiveWorkspace, setActiveWorkspace } from "@/lib/api";
import type { User, Workspace } from "@/lib/types";

interface AuthState {
  user: User | null;
  workspaces: Workspace[];
  workspace: Workspace | null;
  loading: boolean;
  switchWorkspace: (id: string) => Promise<void>;
  reload: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      const me = await api.me();
      const list = await api.listWorkspaces();
      setUser(me);
      setWorkspaces(list);
      const stored = getActiveWorkspace();
      const chosen =
        list.find((w) => w.id === stored) ?? list.find((w) => w.id === me.active_workspace_id) ?? list[0] ?? null;
      setActiveWorkspace(chosen?.id ?? null);
      setWorkspaceId(chosen?.id ?? null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace(`/auth/login?next=${encodeURIComponent(pathname)}`);
        return;
      }
      throw err;
    } finally {
      setLoading(false);
    }
  }, [router, pathname]);

  useEffect(() => {
    reload().catch(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const switchWorkspace = useCallback(async (id: string) => {
    setActiveWorkspace(id);
    setWorkspaceId(id);
    await api.activateWorkspace(id).catch(() => undefined);
    setWorkspaces((ws) => ws.map((w) => ({ ...w, is_active: w.id === id })));
  }, []);

  const logout = useCallback(async () => {
    await api.logout().catch(() => undefined);
    setActiveWorkspace(null);
    router.replace("/auth/login");
  }, [router]);

  const value = useMemo<AuthState>(
    () => ({
      user,
      workspaces,
      workspace: workspaces.find((w) => w.id === workspaceId) ?? null,
      loading,
      switchWorkspace,
      reload,
      logout,
    }),
    [user, workspaces, workspaceId, loading, switchWorkspace, reload, logout]
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
