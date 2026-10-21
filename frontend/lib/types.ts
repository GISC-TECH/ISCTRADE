export type Role = "admin" | "trader" | "viewer";
export type AccountStatus = "stopped" | "running" | "paused" | "killed" | "error";
export type ExecutionMode = "paper" | "live";
export type BotState = "idle" | "starting" | "running" | "paused" | "stopping" | "stopped" | "error" | "killed";

export interface User {
  id: string;
  email: string;
  full_name: string | null;
  role: Role;
  is_active: boolean;
  active_workspace_id: string | null;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface Workspace {
  id: string;
  name: string;
  owner_id: string;
  created_at: string;
  role: Role | null;
  is_active: boolean;
}

export interface Member {
  user_id: string;
  email: string;
  full_name: string | null;
  role: Role;
  is_owner: boolean;
}

export interface Account {
  id: string;
  workspace_id: string;
  label: string;
  api_key_last4: string;
  is_testnet: boolean;
  execution_mode: ExecutionMode;
  status: AccountStatus;
  status_reason: string | null;
  max_leverage: number;
  max_risk_per_trade_pct: string;
  max_daily_loss_pct: string;
  max_drawdown_pct: string;
  paper_balance: string;
  paper_initial_balance: string;
  last_equity: string | null;
  created_at: string;
}

export interface AccountCreate {
  label: string;
  api_key: string;
  api_secret: string;
  is_testnet: boolean;
  execution_mode: ExecutionMode;
  paper_initial_balance: number;
  max_leverage: number;
  max_risk_per_trade_pct: number;
  max_daily_loss_pct: number;
  max_drawdown_pct: number;
}

export interface PositionOut {
  symbol: string;
  quantity: string;
  entry_price: string;
  mark_price: string | null;
  unrealized_pnl: string;
  leverage: number;
  stop_loss: string | null;
}

export interface AccountDashboard {
  account_id: string;
  source: "paper" | "exchange" | "database";
  status: AccountStatus;
  status_reason: string | null;
  execution_mode: ExecutionMode;
  is_testnet: boolean;
  wallet_balance: string;
  available_balance: string;
  unrealized_pnl: string;
  equity: string;
  realized_pnl_today: string;
  total_pnl: string | null;
  open_positions: number;
  positions: PositionOut[];
  peak_equity: string | null;
  day_start_equity: string | null;
  drawdown_pct: number;
  error: string | null;
}

export interface ParamSpec {
  name: string;
  type: "number" | "integer" | "boolean";
  default: number | boolean | null;
  description: string;
  required: boolean;
  min: number | null;
  max: number | null;
}

export interface StrategyInfo {
  key: string;
  name: string;
  description: string;
  params: ParamSpec[];
}

export interface Bot {
  id: string;
  workspace_id: string;
  account_id: string;
  name: string;
  strategy_key: string;
  symbol: string;
  interval: string;
  leverage: number;
  params: Record<string, unknown>;
  options: Partial<BotOptions>;
  state: BotState;
  runtime: Record<string, unknown>;
  is_enabled: boolean;
  last_tick_at: string | null;
  last_error: string | null;
  created_at: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Order {
  id: string;
  account_id: string;
  bot_id: string | null;
  symbol: string;
  side: "BUY" | "SELL";
  type: string;
  status: string;
  quantity: string;
  price: string | null;
  stop_loss: string | null;
  leverage: number;
  reduce_only: boolean;
  executed_qty: string;
  avg_price: string | null;
  is_paper: boolean;
  reason: string | null;
  created_at: string;
}

export interface Trade {
  id: string;
  account_id: string;
  symbol: string;
  side: "BUY" | "SELL";
  price: string;
  quantity: string;
  fee: string;
  realized_pnl: string;
  is_paper: boolean;
  executed_at: string;
}

export interface EquityPoint {
  account_id: string;
  wallet_balance: string;
  unrealized_pnl: string;
  equity: string;
  is_paper: boolean;
  taken_at: string;
}

export interface AuditEntry {
  id: string;
  created_at: string;
  event: string;
  level: string;
  user_id: string | null;
  account_id: string | null;
  ip_address: string | null;
  data: Record<string, unknown>;
}

export interface BacktestConfig {
  initial_balance: number;
  fee_rate: number;
  slippage_bps: number;
  leverage: number;
  max_risk_per_trade_pct: number;
  max_leverage: number;
  max_daily_loss_pct: number;
  max_drawdown_pct: number;
  stop_on_kill_switch: boolean;
}

export interface BacktestRequest {
  strategy_key: string;
  params: Record<string, unknown>;
  symbol: string;
  interval: string;
  start?: string;
  end?: string;
  candles_csv?: string;
  use_testnet_data?: boolean;
  config: Partial<BacktestConfig>;
}

export interface BacktestMetrics {
  initial_balance: number;
  final_equity: number;
  net_pnl: number;
  return_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
  num_fills: number;
  num_closed_trades: number;
  win_rate_pct: number;
  profit_factor: number | null;
  total_fees: number;
  buy_hold_return_pct: number;
  candles: number;
  start: string;
  end: string;
  rejections?: Record<string, number>;
  killed_at?: number | null;
}

export interface BacktestRun {
  id: string;
  strategy_key: string;
  symbol: string;
  interval: string;
  status: "pending" | "running" | "completed" | "failed";
  params: Record<string, unknown>;
  metrics: BacktestMetrics;
  created_at: string;
  error: string | null;
  config?: Record<string, unknown>;
  equity_curve?: { time: number; equity: number }[];
  trades?: { time: number; side: string; quantity: string; price: string; fee: string; realized_pnl: string; tag: string }[];
}

export interface AdminStats {
  users: number;
  workspaces: number;
  accounts: number;
  accounts_running: number;
  accounts_killed: number;
  bots: number;
  bots_running: number;
  bots_error: number;
  orders: number;
  trades: number;
  global_kill_switch: { active: boolean; reason?: string | null };
}

export interface AdminWorkspace {
  id: string;
  name: string;
  owner_email: string;
  accounts: number;
  running_accounts: number;
}

// monitoring / alerts
export type AlertSeverity = "info" | "warning" | "critical";
export type AlertStatus = "pending" | "sent" | "partial" | "failed" | "disabled";

export interface AlertChannelStatus {
  channel: string;
  enabled: boolean;
  provider: string;
  provider_known: boolean;
  configured: boolean;
  default_recipients: number;
}

export interface AlertEventInfo {
  key: string;
  label: string;
  severity: AlertSeverity;
  default_enabled: boolean;
}

export interface AlertSettings {
  enabled: boolean;
  recipients: string[];
  events: Record<string, boolean>;
  min_severity: AlertSeverity;
  throttle_minutes: number;
  uses_default_recipients: boolean;
  available_events: AlertEventInfo[];
  channel: AlertChannelStatus;
}

export type AlertSettingsUpdate = Partial<
  Pick<AlertSettings, "enabled" | "recipients" | "events" | "min_severity" | "throttle_minutes">
>;

export interface AlertItem {
  id: string;
  created_at: string;
  workspace_id: string | null;
  account_id: string | null;
  event: string;
  severity: AlertSeverity;
  title: string;
  message: string;
  status: AlertStatus;
  channel: string;
  recipients: string[];
  attempts: number;
  error: string | null;
  sent_at: string | null;
}

export interface HealthCheck {
  ok: boolean;
  latency_ms?: number;
  error?: string;
  skipped?: string;
  state?: string;
  age_seconds?: number | null;
}

export interface MonitoringStatus {
  readiness: { status: "ready" | "degraded" | "not_ready"; ready: boolean; checks: Record<string, HealthCheck> };
  channel: AlertChannelStatus;
  alerts_24h: number;
  alerts_failed_24h: number;
  alerts_enabled: boolean;
  metrics_enabled: boolean;
}

// bot options / scanner
export interface AutoSymbolsOptions {
  enabled: boolean;
  top_n: number;
  max_positions: number;
  min_score: number;
  prefer_signals: boolean;
}

export interface BotOptions {
  auto_symbols: AutoSymbolsOptions;
}

export interface ScannerRow {
  symbol: string;
  rank: number;
  price: number;
  change_pct_24h: number;
  quote_volume: number;
  funding_rate_pct: number;
  atr_pct: number;
  adx: number;
  rsi: number | null;
  trend: "up" | "down" | "flat";
  momentum_pct: number;
  volume_spike: number;
  signals: Record<string, "long" | "short">;
  bias: "long" | "short" | "neutral";
  components: Record<string, number>;
  score: number;
}

export interface ScannerResponse {
  scanned_at: string | null;
  interval: string;
  testnet: boolean;
  symbols_scanned: number;
  total: number;
  duration_ms: number;
  weights: Record<string, number>;
  strategies: string[];
  results: ScannerRow[];
}
