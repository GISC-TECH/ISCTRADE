// Minimal API client. Auth uses httpOnly cookies set by the backend, so we send credentials.
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let activeWorkspaceId: string | null = null;
export function setActiveWorkspace(id: string | null) {
  activeWorkspaceId = id;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (activeWorkspaceId) headers.set("X-Workspace-ID", activeWorkspaceId);

  const res = await fetch(`${API_URL}${path}`, { ...init, headers, credentials: "include" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export type User = { id: string; email: string; full_name: string | null; role: string; is_active: boolean };
export type Account = {
  id: string;
  workspace_id: string;
  label: string;
  api_key_last4: string;
  is_testnet: boolean;
  status: "stopped" | "running" | "paused" | "killed" | "error";
};

export const api = {
  register: (email: string, password: string, full_name?: string) =>
    request<User>("/auth/register", { method: "POST", body: JSON.stringify({ email, password, full_name }) }),
  login: (email: string, password: string) =>
    request<{ access_token: string }>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: () => request<User>("/auth/me"),
  listAccounts: () => request<Account[]>("/accounts/"),
  connectAccount: (data: { label: string; api_key: string; api_secret: string; is_testnet: boolean }) =>
    request<Account>("/accounts/", { method: "POST", body: JSON.stringify(data) }),
  accountDashboard: (id: string) => request<Record<string, unknown>>(`/accounts/${id}/dashboard`),
  startAccount: (id: string) => request<Account>(`/accounts/${id}/start`, { method: "POST" }),
  stopAccount: (id: string) => request<Account>(`/accounts/${id}/stop`, { method: "POST" }),
};
