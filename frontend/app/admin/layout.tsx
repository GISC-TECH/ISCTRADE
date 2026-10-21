import { AppShell } from "@/components/app-shell";
import { AuthProvider } from "@/components/auth-provider";
import { RealtimeProvider } from "@/components/realtime-provider";

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <RealtimeProvider>
        <AppShell>{children}</AppShell>
      </RealtimeProvider>
    </AuthProvider>
  );
}
