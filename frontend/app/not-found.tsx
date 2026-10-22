import Link from "next/link";

import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <main id="conteudo" className="container flex min-h-screen flex-col items-center justify-center gap-4 text-center">
      <p className="text-sm font-semibold text-muted-foreground">Erro 404</p>
      <h1 className="text-3xl font-bold">Página não encontrada</h1>
      <p className="max-w-md text-muted-foreground">
        O endereço acessado não existe ou foi movido. Verifique o link ou volte para o painel.
      </p>
      <div className="flex gap-3">
        <Button asChild>
          <Link href="/dashboard">Ir para o painel</Link>
        </Button>
        <Button asChild variant="outline">
          <Link href="/">Página inicial</Link>
        </Button>
      </div>
    </main>
  );
}
