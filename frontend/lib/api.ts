// API client. Auth uses httpOnly cookies set by the backend (credentials: "include").
// The active workspace is sent in the X-Workspace-ID header.
import type {
  Account,
  AccountCreate,
  AccountDashboard,
  AdminStats,
  AdminWorkspace,
  AlertItem,
  AlertSettings,
  AlertSettingsUpdate,
  AssetSentiment,
  AuditEntry,
  BacktestRequest,
  WalkForwardRequest,
  BacktestRun,
  Bot,
  BotOptions,
  BotPreset,
  Candle,
  CloseAllResult,
  EconomicEvent,
  EquityPoint,
  ExchangeInfo,
  ManualOrderCreate,
  ManualOrderResult,
  MfaChallenge,
  MfaSetup,
  MfaStatus,
  Member,
  MonitoringStatus,
  NewsItem,
  NewsStatus,
  Order,
  Page,
  PnlReport,
  PositionOut,
  Role,
  ScannerResponse,
  SecurityPolicy,
  StrategyInfo,
  TokenResponse,
  Trade,
  User,
  Workspace,
} from "./types";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api/v1";
const WORKSPACE_KEY = "btp.workspace";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string
  ) {
    super(message);
  }
}

export function getActiveWorkspace(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(WORKSPACE_KEY);
}

export function setActiveWorkspace(id: string | null) {
  if (typeof window === "undefined") return;
  if (id) window.localStorage.setItem(WORKSPACE_KEY, id);
  else window.localStorage.removeItem(WORKSPACE_KEY);
}

type Query = Record<string, string | number | boolean | null | undefined>;

function qs(params?: Query): string {
  if (!params) return "";
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

let refreshing: Promise<boolean> | null = null;
// 401 on these means "wrong credentials/expired step", not an expired access token.
const NO_REFRESH_PATHS = ["/auth/login", "/auth/register", "/auth/refresh", "/auth/logout"];
export const MFA_ENROLL_PATH = "/dashboard/security";
export const MFA_REQUIRED_EVENT = "btp:mfa-required";

export async function tryRefresh(): Promise<boolean> {
  if (!refreshing) {
    refreshing = fetch(`${API_URL}/auth/refresh`, { method: "POST", credentials: "include" })
      .then((r) => r.ok)
      .catch(() => false)
      .finally(() => {
        setTimeout(() => (refreshing = null), 0);
      });
  }
  return refreshing;
}

async function request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const ws = getActiveWorkspace();
  if (ws && !headers.has("X-Workspace-ID")) headers.set("X-Workspace-ID", ws);

  const res = await fetch(`${API_URL}${path}`, { ...init, headers, credentials: "include" });
  if (res.status === 401 && retry && !NO_REFRESH_PATHS.some((p) => path.startsWith(p))) {
    if (await tryRefresh()) return request<T>(path, init, false);
  }
  if (res.status === 404 && ws && retry && res.headers.get("content-type")?.includes("json")) {
    // stale workspace id (e.g. removed from workspace): retry once without it
    const body = await res.clone().json().catch(() => null);
    if (body?.detail === "Workspace não encontrado") {
      setActiveWorkspace(null);
      return request<T>(path, init, false);
    }
  }
  if (res.status === 403 && res.headers.get("x-mfa-enrollment-required") && typeof window !== "undefined") {
    // 2FA is mandatory for this user and not enrolled yet: AuthProvider routes them to Segurança.
    window.dispatchEvent(new CustomEvent(MFA_REQUIRED_EVENT, { detail: res.headers.get("x-mfa-enrollment-required") }));
  }
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore */
    }
    const message = Array.isArray(detail)
      ? detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join("; ")
      : typeof detail === "string"
        ? detail
        : JSON.stringify(detail);
    throw new ApiError(res.status, message);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

/** Authenticated file download (CSV exports): fetch with cookies + workspace header, then save via a blob URL. */
async function download(path: string, retry = true): Promise<void> {
  const headers = new Headers();
  const ws = getActiveWorkspace();
  if (ws) headers.set("X-Workspace-ID", ws);
  const res = await fetch(`${API_URL}${path}`, { headers, credentials: "include" });
  if (res.status === 401 && retry && (await tryRefresh())) return download(path, false);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(res.status, typeof body?.detail === "string" ? body.detail : "Falha ao exportar");
  }
  const disposition = res.headers.get("content-disposition") ?? "";
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? "export.csv";
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const patch = <T>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body: JSON.stringify(body) });
const put = <T>(path: string, body: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body) });
const del = (path: string) => request<void>(path, { method: "DELETE" });

export const api = {
  // auth
  register: (email: string, password: string, full_name?: string) =>
    post<User>("/auth/register", { email, password, full_name }),
  login: (email: string, password: string) => post<TokenResponse | MfaChallenge>("/auth/login", { email, password }),
  login2fa: (mfa_token: string, code: string) => post<TokenResponse>("/auth/login/2fa", { mfa_token, code }),
  acceptDisclaimer: (version: string) => post<User>("/auth/me/disclaimer", { version }),
  mfaStatus: () => request<MfaStatus>("/auth/2fa"),
  mfaSetup: () => post<MfaSetup>("/auth/2fa/setup"),
  mfaEnable: (code: string) => post<{ recovery_codes: string[] }>("/auth/2fa/enable", { code }),
  mfaDisable: (password: string, code: string) => post<MfaStatus>("/auth/2fa/disable", { password, code }),
  mfaRecoveryCodes: (password: string, code: string) =>
    post<{ recovery_codes: string[] }>("/auth/2fa/recovery-codes", { password, code }),
  logout: () => post<void>("/auth/logout"),
  logoutAll: () => post<void>("/auth/logout-all"),
  me: () => request<User>("/auth/me"),
  updateMe: (data: { full_name: string | null }) => patch<User>("/auth/me", data),
  changePassword: (current_password: string, new_password: string) =>
    post<TokenResponse>("/auth/me/password", { current_password, new_password }),

  // workspaces
  listWorkspaces: () => request<Workspace[]>("/workspaces/"),
  createWorkspace: (name: string) => post<Workspace>("/workspaces/", { name }),
  renameWorkspace: (id: string, name: string) => patch<Workspace>(`/workspaces/${id}`, { name }),
  deleteWorkspace: (id: string) => del(`/workspaces/${id}`),
  activateWorkspace: (id: string) => post<Workspace>(`/workspaces/${id}/activate`),
  listMembers: (id: string) => request<Member[]>(`/workspaces/${id}/members`),
  addMember: (id: string, email: string, role: Role) => post<Member>(`/workspaces/${id}/members`, { email, role }),
  updateMember: (id: string, userId: string, role: Role) =>
    patch<Member>(`/workspaces/${id}/members/${userId}`, { role }),
  removeMember: (id: string, userId: string) => del(`/workspaces/${id}/members/${userId}`),

  // accounts
  listAccounts: () => request<Account[]>("/accounts/"),
  listExchanges: () => request<ExchangeInfo[]>("/accounts/exchanges"),
  connectAccount: (data: AccountCreate) => post<Account>("/accounts/", data),
  updateAccount: (id: string, data: Partial<AccountCreate>) => patch<Account>(`/accounts/${id}`, data),
  deleteAccount: (id: string) => del(`/accounts/${id}`),
  accountDashboard: (id: string) => request<AccountDashboard>(`/accounts/${id}/dashboard`),
  verifyAccount: (id: string) =>
    post<{ ok: boolean; can_trade: boolean | null; wallet_balance: string | null; hedge_mode?: boolean | null; error: string | null }>(
      `/accounts/${id}/verify`
    ),
  startAccount: (id: string) => post<Account>(`/accounts/${id}/start`),
  stopAccount: (id: string) => post<Account>(`/accounts/${id}/stop`),
  resetKillSwitch: (id: string) => post<Account>(`/accounts/${id}/reset-kill-switch`),

  // bots
  listBots: (account_id?: string) => request<Bot[]>(`/bots/${qs({ account_id })}`),
  createBot: (data: {
    account_id: string;
    name: string;
    strategy_key: string;
    symbol: string;
    interval: string;
    leverage: number;
    params: Record<string, unknown>;
    options?: BotOptions;
  }) => post<Bot>("/bots/", data),
  updateBot: (
    id: string,
    data: Partial<Pick<Bot, "name" | "interval" | "leverage" | "params" | "is_enabled">> & { options?: BotOptions }
  ) => patch<Bot>(`/bots/${id}`, data),
  deleteBot: (id: string) => del(`/bots/${id}`),
  botAction: (id: string, action: "start" | "pause" | "resume" | "stop" | "reset-state") =>
    post<Bot>(`/bots/${id}/${action}`),

  // strategies / backtests
  listStrategies: () => request<StrategyInfo[]>("/strategies/"),
  listPresets: () => request<BotPreset[]>("/strategies/presets"),
  runBacktest: (data: BacktestRequest) => post<BacktestRun>("/strategies/backtest", data),
  runWalkForward: (data: WalkForwardRequest) => post<BacktestRun>("/strategies/walk-forward", data),
  listBacktests: () => request<BacktestRun[]>("/strategies/backtests"),
  getBacktest: (id: string) => request<BacktestRun>(`/strategies/backtests/${id}`),

  // history
  orders: (params?: Query) => request<Page<Order>>(`/history/orders${qs(params)}`),
  trades: (params?: Query) => request<Page<Trade>>(`/history/trades${qs(params)}`),
  equity: (params?: Query) => request<EquityPoint[]>(`/history/equity${qs(params)}`),
  positions: (params?: Query) =>
    request<(PositionOut & { account_id: string; is_paper?: boolean; updated_at?: string })[]>(
      `/history/positions${qs(params)}`
    ),
  audit: (params?: Query) => request<Page<AuditEntry>>(`/history/audit${qs(params)}`),
  pnl: (params?: Query) => request<PnlReport>(`/history/pnl${qs(params)}`),
  downloadCsv: (kind: "trades" | "income", params?: Query) => download(`/history/export/${kind}.csv${qs(params)}`),

  // scanner
  scanner: (params?: Query) => request<ScannerResponse>(`/scanner/${qs(params)}`),
  refreshScanner: () => post<{ queued: boolean }>("/scanner/refresh"),

  // news & economic calendar
  news: (params?: Query) => request<Page<NewsItem>>(`/news/${qs(params)}`),
  newsSentiment: (symbols?: string[]) =>
    request<AssetSentiment[]>(`/news/sentiment${qs({ symbols: symbols?.length ? symbols.join(",") : undefined })}`),
  newsStatus: () => request<NewsStatus>("/news/status"),
  refreshNews: () => post<{ queued: boolean }>("/news/refresh"),
  calendar: (params?: Query) => request<EconomicEvent[]>(`/calendar/${qs(params)}`),
  refreshCalendar: () => post<{ queued: boolean }>("/calendar/refresh"),

  // real-time panel: market history + manual trading
  klines: (params: { symbol: string; interval: string; limit?: number; testnet?: boolean }) =>
    request<Candle[]>(`/market/klines${qs(params)}`),
  tradingStatus: () => request<{ global_kill_switch: boolean }>("/trading/status"),
  placeManualOrder: (data: ManualOrderCreate) => post<ManualOrderResult>("/trading/orders", data),
  cancelManualOrder: (orderId: string, account_id: string) =>
    request<Order>(`/trading/orders/${orderId}${qs({ account_id })}`, { method: "DELETE" }),
  closePosition: (account_id: string, symbol: string) =>
    post<ManualOrderResult>("/trading/close", { account_id, symbol, confirm: true }),
  closeAll: (account_id: string) => post<CloseAllResult>("/trading/close-all", { account_id, confirm: true }),
  setLeverage: (account_id: string, symbol: string, leverage: number) =>
    post<{ symbol: string; leverage: number }>("/trading/leverage", { account_id, symbol, leverage, confirm: true }),

  // monitoring / alerts
  monitoringStatus: () => request<MonitoringStatus>("/monitoring/status"),
  alertSettings: () => request<AlertSettings>("/alerts/settings"),
  updateAlertSettings: (data: AlertSettingsUpdate) => put<AlertSettings>("/alerts/settings", data),
  sendTestAlert: (message?: string) => post<AlertItem>("/alerts/test", message ? { message } : {}),
  alerts: (params?: Query) => request<Page<AlertItem>>(`/alerts/${qs(params)}`),

  // admin
  adminStats: () => request<AdminStats>("/admin/stats"),
  adminUsers: (params?: Query) => request<Page<User>>(`/admin/users${qs(params)}`),
  adminUpdateUser: (id: string, data: { role?: Role; is_active?: boolean }) => patch<User>(`/admin/users/${id}`, data),
  adminWorkspaces: (params?: Query) => request<Page<AdminWorkspace>>(`/admin/workspaces${qs(params)}`),
  adminKillSwitch: (active: boolean, reason?: string) =>
    post<{ active: boolean; reason?: string }>("/admin/kill-switch", { active, reason }),
  adminAudit: (params?: Query) => request<Page<AuditEntry>>(`/admin/audit${qs(params)}`),
  adminSecurityPolicy: () => request<SecurityPolicy>("/admin/security-policy"),
  adminUpdateSecurityPolicy: (require_2fa_all: boolean) =>
    put<SecurityPolicy>("/admin/security-policy", { require_2fa_all }),
  adminReset2fa: (id: string) => post<User>(`/admin/users/${id}/reset-2fa`),
};

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "Erro inesperado";
}
