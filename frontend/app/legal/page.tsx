import type { Metadata } from "next";
import Link from "next/link";

import { DisclaimerText } from "@/components/legal";

export const metadata: Metadata = { title: "Aviso legal · ISCTRADE" };

export default function LegalPage() {
  return (
    <main id="conteudo" className="container max-w-3xl py-12">
      <h1 className="mb-6 text-3xl font-bold">Aviso regulatório, tributário e de risco</h1>
      <DisclaimerText />
      <p className="mt-8 text-sm">
        <Link href="/" className="underline underline-offset-2">
          Voltar ao início
        </Link>
      </p>
    </main>
  );
}
