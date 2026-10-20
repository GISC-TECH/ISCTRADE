import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Binance Trading Platform",
  description: "Plataforma self-hosted de trading para Binance USDT-M Futures",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="pt-BR" className="dark">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
