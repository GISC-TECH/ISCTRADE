import Link from "next/link";

import { LegalFooter } from "@/components/legal";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const features = [
  { title: "Gestão de risco", description: "Stop loss obrigatório, limite de alavancagem, perda diária máxima e kill switch." },
  { title: "Multi-conta", description: "Workspaces isolados com chaves de API criptografadas (AES-256-GCM)." },
  { title: "Auto-hospedado", description: "Seus dados e suas chaves ficam no seu servidor." },
];

export default function HomePage() {
  return (
    <>
    <main id="conteudo" className="container flex min-h-screen flex-col items-center justify-center gap-10 py-16 text-center">
      <div className="space-y-4">
        <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">ISCTRADE</h1>
        <p className="mx-auto max-w-2xl text-lg text-muted-foreground">
          Plataforma auto-hospedada para operar futuros USDT-M (Binance) com automação, monitoramento e regras de risco
          determinísticas.
        </p>
        <div className="flex justify-center gap-3">
          <Button asChild size="lg">
            <Link href="/auth/register">Começar</Link>
          </Button>
          <Button asChild size="lg" variant="outline">
            <Link href="/auth/login">Entrar</Link>
          </Button>
        </div>
      </div>
      <div className="grid w-full max-w-4xl gap-4 sm:grid-cols-3">
        {features.map((f) => (
          <Card key={f.title} className="text-left">
            <CardHeader>
              <CardTitle className="text-lg">{f.title}</CardTitle>
              <CardDescription>{f.description}</CardDescription>
            </CardHeader>
          </Card>
        ))}
      </div>
      <Card className="max-w-2xl border-destructive/50">
        <CardContent className="pt-6 text-sm text-muted-foreground">
          ⚠️ Operar derivativos alavancados envolve alto risco de perda total do capital. Use por sua conta e risco.{" "}
          <Link href="/legal" className="underline underline-offset-2">
            Leia o aviso regulatório e tributário
          </Link>
          .
        </CardContent>
      </Card>
    </main>
    <LegalFooter />
    </>
  );
}
