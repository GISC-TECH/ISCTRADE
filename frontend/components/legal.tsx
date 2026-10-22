"use client";

import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { api, errorMessage } from "@/lib/api";
import type { User } from "@/lib/types";

/** Must match backend app/core/legal.py DISCLAIMER_VERSION (bump both to ask everyone again). */
export const DISCLAIMER_VERSION = "2026-10";

export function DisclaimerText() {
  return (
    <div className="space-y-3 text-sm leading-relaxed">
      <p>
        <strong>O ISCTRADE é um software de automação auto-hospedado.</strong> Não é corretora, não custodia recursos e
        não presta consultoria, recomendação de investimento, nem serviços jurídicos ou contábeis.
      </p>
      <p>
        <strong>Risco.</strong> Futuros perpétuos de criptoativos são derivativos alavancados e de alto risco: você pode
        perder todo o capital alocado em pouco tempo. Resultados de backtest ou de paper trading não garantem resultados
        futuros.
      </p>
      <p>
        <strong>Regulação (residentes no Brasil).</strong> A prestação de serviços de ativos virtuais no Brasil é
        regulada pelo Banco Central do Brasil (Resoluções BCB nº 519, 520 e 521), e operações com derivativos podem
        estar sujeitas à regulamentação da Comissão de Valores Mobiliários (CVM). Antes de operar com dinheiro real,
        verifique se a corretora utilizada está autorizada a atender residentes no Brasil e se o produto é permitido
        para você.
      </p>
      <p>
        <strong>Tributação.</strong> Ganhos com criptoativos e com operações em plataformas no exterior podem gerar
        imposto e obrigações acessórias (por exemplo, declarações periódicas à Receita Federal e informação na
        declaração anual). As regras mudam com frequência: qualquer referência tributária exibida na plataforma é apenas
        indicativa e <strong>deve ser confirmada com um contador ou advogado tributarista</strong>. Use a exportação
        CSV (PnL &amp; relatórios) para guardar o histórico das suas operações.
      </p>
      <p className="text-muted-foreground">
        Este aviso não constitui parecer jurídico, tributário ou recomendação de investimento.
      </p>
    </div>
  );
}

export function LegalFooter() {
  return (
    <footer className="border-t px-4 py-3 text-center text-xs text-muted-foreground">
      Não é recomendação de investimento nem parecer jurídico ou tributário. Derivativos alavancados podem causar perda
      total. Residentes no Brasil: observe as Resoluções BCB nº 519/520/521 e as normas da CVM; confirme a tributação
      com um profissional.{" "}
      <Link href="/legal" className="underline underline-offset-2 hover:text-foreground">
        Aviso completo
      </Link>
    </footer>
  );
}

/** Blocking first-login acknowledgement of the regulatory/tax disclaimer. */
export function DisclaimerGate({ user, onAccepted }: { user: User; onAccepted: () => void }) {
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!user.disclaimer_pending) return null;

  async function accept() {
    setBusy(true);
    setError(null);
    try {
      await api.acceptDisclaimer(DISCLAIMER_VERSION);
      onAccepted();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/80 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="disclaimer-title"
        className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-lg border bg-background p-6 shadow-lg"
      >
        <h2 id="disclaimer-title" className="mb-4 text-xl font-semibold">
          Aviso regulatório, tributário e de risco
        </h2>
        <DisclaimerText />
        <label className="mt-5 flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            autoFocus
            checked={checked}
            onChange={(e) => setChecked(e.target.checked)}
          />
          Li e entendi o aviso acima, inclusive que devo confirmar regras regulatórias e tributárias com profissionais
          habilitados.
        </label>
        {error && (
          <p role="alert" className="mt-3 text-sm text-red-400">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end">
          <Button onClick={accept} disabled={!checked || busy}>
            {busy ? "Registrando..." : "Concordo e continuar"}
          </Button>
        </div>
      </div>
    </div>
  );
}
