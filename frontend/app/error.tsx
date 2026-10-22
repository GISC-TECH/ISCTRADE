"use client";

import Link from "next/link";
import { useEffect } from "react";

import { Button } from "@/components/ui/button";

export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div role="alert" className="flex min-h-[60vh] flex-col items-center justify-center gap-4 p-6 text-center">
      <p className="text-sm font-semibold text-muted-foreground">Erro 500</p>
      <h1 className="text-3xl font-bold">Algo deu errado</h1>
      <p className="max-w-md text-muted-foreground">
        Ocorreu um erro inesperado ao exibir esta página. Nenhuma ordem foi enviada por causa deste erro de tela. Tente
        novamente; se persistir, verifique os logs do servidor.
      </p>
      {error.digest && <p className="font-mono text-xs text-muted-foreground">Código do erro: {error.digest}</p>}
      <div className="flex gap-3">
        <Button onClick={() => reset()}>Tentar novamente</Button>
        <Button asChild variant="outline">
          <Link href="/dashboard">Ir para o painel</Link>
        </Button>
      </div>
    </div>
  );
}
