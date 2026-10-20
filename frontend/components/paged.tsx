"use client";

import { useCallback, useEffect, useState } from "react";

import { EmptyState, Pager } from "@/components/common";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { errorMessage } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { AuditEntry, Page } from "@/lib/types";

export const LIMIT = 25;

export function usePaged<T>(fetcher: (offset: number) => Promise<Page<T>>, deps: unknown[]) {
  const [page, setPage] = useState<Page<T> | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(
    (o: number) => {
      fetcher(o)
        .then((p) => {
          setPage(p);
          setError(null);
        })
        .catch((e) => setError(errorMessage(e)));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    deps
  );
  useEffect(() => {
    setOffset(0);
    load(0);
  }, [load]);
  return {
    page,
    error,
    offset,
    go: (o: number) => {
      setOffset(o);
      load(o);
    },
  };
}

export function AuditTable({ fetcher, deps }: { fetcher: (o: number) => Promise<Page<AuditEntry>>; deps: unknown[] }) {
  const { page, error, offset, go } = usePaged<AuditEntry>(fetcher, deps);
  if (error) return <Alert variant="destructive">{error}</Alert>;
  if (!page) return <EmptyState>Carregando...</EmptyState>;
  if (!page.items.length) return <EmptyState>Nenhum evento.</EmptyState>;
  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Data</TableHead>
            <TableHead>Evento</TableHead>
            <TableHead>Nível</TableHead>
            <TableHead>IP</TableHead>
            <TableHead>Detalhes</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {page.items.map((a) => (
            <TableRow key={a.id}>
              <TableCell className="whitespace-nowrap text-xs">{dateTime(a.created_at)}</TableCell>
              <TableCell className="font-mono text-xs">{a.event}</TableCell>
              <TableCell>
                <Badge variant={a.level === "critical" || a.level === "error" ? "destructive" : a.level === "warning" ? "warning" : "secondary"}>
                  {a.level}
                </Badge>
              </TableCell>
              <TableCell className="text-xs">{a.ip_address ?? "—"}</TableCell>
              <TableCell className="max-w-md truncate font-mono text-xs text-muted-foreground" title={JSON.stringify(a.data)}>
                {JSON.stringify(a.data)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Pager total={page.total} limit={LIMIT} offset={offset} onChange={go} />
    </>
  );
}

