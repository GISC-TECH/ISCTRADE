"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { AccountStatus, BotState } from "@/lib/types";

const statusVariant: Record<string, "success" | "secondary" | "destructive" | "warning" | "outline"> = {
  running: "success",
  starting: "warning",
  stopping: "warning",
  paused: "warning",
  stopped: "secondary",
  idle: "secondary",
  killed: "destructive",
  error: "destructive",
};

const statusLabel: Record<string, string> = {
  running: "Em execução",
  starting: "Iniciando",
  stopping: "Parando",
  paused: "Pausado",
  stopped: "Parado",
  idle: "Ocioso",
  killed: "Kill switch",
  error: "Erro",
};

export function StatusBadge({ status }: { status: AccountStatus | BotState | string }) {
  return <Badge variant={statusVariant[status] ?? "outline"}>{statusLabel[status] ?? status}</Badge>;
}

export function PageHeader({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 className="text-2xl font-bold">{title}</h1>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
      {children && <div className="flex flex-wrap gap-2">{children}</div>}
    </div>
  );
}

export function Pager({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
}) {
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="mt-3 flex items-center justify-between text-sm text-muted-foreground">
      <span>
        {total} registro(s) — página {page} de {pages}
      </span>
      <div className="flex gap-2">
        <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
          Anterior
        </Button>
        <Button variant="outline" size="sm" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
          Próxima
        </Button>
      </div>
    </div>
  );
}

export function EmptyState({ children }: { children: React.ReactNode }) {
  return <p className="py-8 text-center text-sm text-muted-foreground">{children}</p>;
}
