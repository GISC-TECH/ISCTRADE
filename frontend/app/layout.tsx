import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ISCTRADE — trading de futuros",
  description: "Plataforma auto-hospedada de trading automatizado de futuros USDT-M (Binance)",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="pt-BR" className="dark">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
