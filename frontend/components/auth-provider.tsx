"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { api, ApiError, getActiveWorkspace, MFA_ENROLL_PATH, MFA_REQUIRED_EVENT, setActiveWorkspace } from "@/lib/api";
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

  // Promise chain (no async/await) so the initial load in the effect only sets state from callbacks.
  const reload = useCallback((): Promise<void> => {
    return api
      .me()
      .then((me) => {
        // Users that must enrol in 2FA first only reach /auth/me and the 2FA endpoints.
        const blocked = me.mfa_required && !me.totp_enabled && me.mfa_reason !== "admin";
        return (blocked ? Promise.resolve<Workspace[]>([]) : api.listWorkspaces()).then((list) => {
          setUser(me);
          setWorkspaces(list);
          const stored = getActiveWorkspace();
          const chosen =
            list.find((w) => w.id === stored) ?? list.find((w) => w.id === me.active_workspace_id) ?? list[0] ?? null;
          if (!blocked) setActiveWorkspace(chosen?.id ?? null);
          setWorkspaceId(chosen?.id ?? null);
        });
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          router.replace(`/auth/login?next=${encodeURIComponent(pathname)}`);
          return;
        }
        throw err;
      })
      .finally(() => setLoading(false));
  }, [router, pathname]);

  useEffect(() => {
    reload().catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The API answered 403 + X-MFA-Enrollment-Required: 2FA is mandatory and not enrolled yet.
  useEffect(() => {
    const onMfaRequired = () => {
      if (!window.location.pathname.startsWith(MFA_ENROLL_PATH)) router.push(`${MFA_ENROLL_PATH}?obrigatorio=1`);
    };
    window.addEventListener(MFA_REQUIRED_EVENT, onMfaRequired);
    return () => window.removeEventListener(MFA_REQUIRED_EVENT, onMfaRequired);
  }, [router]);

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
