"use client";

import "./globals.css";

// Replaces the root layout when it fails, so it must render its own <html>/<body>.
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <html lang="pt-BR" className="dark">
      <body className="min-h-screen antialiased">
        <main role="alert" className="flex min-h-screen flex-col items-center justify-center gap-4 p-6 text-center">
          <p className="text-sm font-semibold text-muted-foreground">Erro 500</p>
          <h1 className="text-3xl font-bold">Erro na aplicação</h1>
          <p className="max-w-md text-muted-foreground">
            A interface não conseguiu carregar. Recarregue a página; se o problema continuar, verifique se a API está
            no ar e consulte os logs do servidor.
          </p>
          {error.digest && <p className="font-mono text-xs text-muted-foreground">Código do erro: {error.digest}</p>}
          <div className="flex gap-3">
            <button
              type="button"
              onClick={() => reset()}
              className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
            >
              Tentar novamente
            </button>
            {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- the router may be broken here */}
            <a href="/" className="rounded-md border px-4 py-2 text-sm font-medium">
              Página inicial
            </a>
          </div>
        </main>
      </body>
    </html>
  );
}
