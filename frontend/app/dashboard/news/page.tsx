"use client";

import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader } from "@/components/common";
import { ImpactBadge, SentimentBadge, SentimentGauge } from "@/components/sentiment";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorMessage } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { AssetSentiment, NewsItem, NewsStatus } from "@/lib/types";
import { safeHref } from "@/lib/utils";

const PAGE = 50;

export default function NewsPage() {
  const { workspace } = useAuth();
  const [items, setItems] = useState<NewsItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [gauges, setGauges] = useState<AssetSentiment[]>([]);
  const [status, setStatus] = useState<NewsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("");
  const [impact, setImpact] = useState("");
  const [sentiment, setSentiment] = useState("");
  const [source, setSource] = useState("");
  const [search, setSearch] = useState("");
  const canTrade = workspace?.role === "admin" || workspace?.role === "trader";

  const load = useCallback(() => {
    api
      .news({
        symbol: symbol || undefined,
        impact: impact || undefined,
        sentiment: sentiment || undefined,
        source: source || undefined,
        search: search || undefined,
        limit: PAGE,
        offset,
      })
      .then((p) => {
        setItems(p.items);
        setTotal(p.total);
        setError(null);
      })
      .catch((e) => setError(errorMessage(e)));
    api.newsSentiment(symbol ? [symbol] : undefined).then(setGauges).catch(() => undefined);
  }, [symbol, impact, sentiment, source, search, offset]);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load, workspace?.id]);
  useEffect(() => {
    api.newsStatus().then(setStatus).catch(() => undefined);
  }, [workspace?.id]);
  useEffect(() => setOffset(0), [symbol, impact, sentiment, source, search]);

  async function refresh() {
    setInfo(null);
    try {
      const r = await api.refreshNews();
      setInfo(r.queued ? "Coleta agendada; o feed atualiza em instantes." : "Fila indisponível no momento.");
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Notícias & sentimento" description="Feed de notícias cripto com análise de sentimento por ativo.">
        {canTrade && (
          <Button variant="outline" onClick={refresh}>
            Coletar agora
          </Button>
        )}
      </PageHeader>
      <Alert variant="warning">
        Notícias são usadas apenas como <b>filtro</b> (bloquear ou reduzir novas entradas), nunca como gatilho de
        operação. Análise automática de sentimento erra com frequência; não há garantia de lucro.
      </Alert>
      {error && <Alert variant="destructive">{error}</Alert>}
      {info && <Alert>{info}</Alert>}

      <Card>
        <CardHeader>
          <CardTitle>Sentimento agregado por ativo</CardTitle>
          <CardDescription>
            Média ponderada por impacto com decaimento exponencial
            {status ? ` (meia-vida ${status.half_life_hours} h, janela ${status.window_hours} h)` : ""}. Análise:{" "}
            {status ? (status.llm_enabled ? `LLM (${status.llm_model})` : "léxico local (sem LLM configurado)") : "—"}
            .
          </CardDescription>
        </CardHeader>
        <CardContent>
          {gauges.length === 0 ? (
            <EmptyState>Sem notícias analisadas na janela.</EmptyState>
          ) : (
            <div className="flex flex-wrap gap-6">
              {gauges.map((g) => (
                <div key={g.symbol} className="flex flex-col items-center">
                  <SentimentGauge score={g.score} confidence={g.confidence} label={g.symbol} />
                  <span className="text-[11px] text-muted-foreground">{g.count} notícias</span>
                  {g.high_impact_negative && (
                    <Badge variant="destructive" className="mt-1" title={g.high_impact_negative.title}>
                      alto impacto negativo
                    </Badge>
                  )}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <div className="space-y-1">
              <Label htmlFor="n-symbol">Símbolo</Label>
              <Input
                id="n-symbol"
                placeholder="BTCUSDT"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase().trim())}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="n-impact">Impacto</Label>
              <Select id="n-impact" value={impact} onChange={(e) => setImpact(e.target.value)}>
                <option value="">Todos</option>
                <option value="high">Alto</option>
                <option value="medium">Médio</option>
                <option value="low">Baixo</option>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="n-sent">Sentimento</Label>
              <Select id="n-sent" value={sentiment} onChange={(e) => setSentiment(e.target.value)}>
                <option value="">Todos</option>
                <option value="positive">Positivo</option>
                <option value="negative">Negativo</option>
                <option value="neutral">Neutro</option>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="n-source">Fonte</Label>
              <Select id="n-source" value={source} onChange={(e) => setSource(e.target.value)}>
                <option value="">Todas</option>
                <option value="rss">RSS</option>
                <option value="cryptopanic">CryptoPanic</option>
                <option value="x">X</option>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="n-search">Buscar</Label>
              <Input id="n-search" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
          </div>
          {status && (
            <p className="text-xs text-muted-foreground">
              Fontes: {status.rss_sources.join(", ") || "nenhum RSS"}
              {status.cryptopanic_enabled ? " · CryptoPanic" : ""}
              {status.x_enabled ? " · X" : ""} · {status.total_items} notícias armazenadas · última:{" "}
              {dateTime(status.last_item_at)}
            </p>
          )}
          {items === null ? (
            <EmptyState>Carregando...</EmptyState>
          ) : items.length === 0 ? (
            <EmptyState>Nenhuma notícia com esses filtros.</EmptyState>
          ) : (
            <ul className="divide-y">
              {items.map((n) => (
                <li key={n.id} className="space-y-1 py-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <SentimentBadge value={n.sentiment} />
                    <ImpactBadge impact={n.impact} />
                    {n.symbols.map((s) => (
                      <Badge key={s} variant="outline" className="cursor-pointer" onClick={() => setSymbol(s)}>
                        {s}
                      </Badge>
                    ))}
                    <span className="text-xs text-muted-foreground">
                      {n.source_name} · {dateTime(n.published_at)}
                      {n.analysis_method ? ` · ${n.analysis_method === "llm" ? "LLM" : "léxico"}` : ""}
                    </span>
                  </div>
                  <div className="font-medium">
                    {safeHref(n.url) ? (
                      <a href={safeHref(n.url)!} target="_blank" rel="noopener noreferrer nofollow" className="hover:underline">
                        {n.title}
                      </a>
                    ) : (
                      n.title
                    )}
                  </div>
                  {n.summary_pt && <p className="text-sm text-muted-foreground">{n.summary_pt}</p>}
                </li>
              ))}
            </ul>
          )}
          {total > PAGE && (
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">
                {offset + 1}–{Math.min(offset + PAGE, total)} de {total}
              </span>
              <div className="space-x-2">
                <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(offset - PAGE)}>
                  Anterior
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={offset + PAGE >= total}
                  onClick={() => setOffset(offset + PAGE)}
                >
                  Próxima
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
