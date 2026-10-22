"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { PageHeader } from "@/components/common";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorMessage, setActiveWorkspace } from "@/lib/api";
import { dateTime } from "@/lib/format";

/** Remounted (key) when the saved name changes, so the input starts from the server value without an effect. */
function NameForm({ initial, onSave }: { initial: string; onSave: (name: string) => void }) {
  const [fullName, setFullName] = useState(initial);
  return (
    <form
      className="flex items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        onSave(fullName);
      }}
    >
      <div className="flex-1 space-y-1">
        <Label htmlFor="p-name">Nome</Label>
        <Input id="p-name" autoComplete="name" value={fullName} onChange={(e) => setFullName(e.target.value)} />
      </div>
      <Button type="submit">Salvar</Button>
    </form>
  );
}

export default function ProfilePage() {
  const { user, reload } = useAuth();
  const router = useRouter();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  async function run(fn: () => Promise<unknown>, ok: string) {
    setMsg(null);
    try {
      await fn();
      setMsg({ ok: true, text: ok });
    } catch (e) {
      setMsg({ ok: false, text: errorMessage(e) });
    }
  }

  if (!user) return null;
  return (
    <div className="max-w-xl space-y-6">
      <PageHeader title="Perfil" description={`${user.email} · cadastrado em ${dateTime(user.created_at)}`} />
      {msg && <Alert variant={msg.ok ? "success" : "destructive"}>{msg.text}</Alert>}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Dados pessoais</CardTitle>
        </CardHeader>
        <CardContent>
          <NameForm
            key={user.full_name ?? ""}
            initial={user.full_name ?? ""}
            onSave={(fullName) =>
              run(async () => {
                await api.updateMe({ full_name: fullName || null });
                await reload();
              }, "Perfil atualizado.")
            }
          />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Alterar senha</CardTitle>
          <CardDescription>Alterar a senha encerra as sessões em outros dispositivos.</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (next !== confirmPw) {
                setMsg({ ok: false, text: "A confirmação não confere." });
                return;
              }
              run(async () => {
                await api.changePassword(current, next);
                setCurrent("");
                setNext("");
                setConfirmPw("");
              }, "Senha alterada.");
            }}
          >
            <div className="space-y-1">
              <Label htmlFor="p-cur">Senha atual</Label>
              <Input id="p-cur" type="password" required autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="p-new">Nova senha (mín. 8 caracteres)</Label>
              <Input id="p-new" type="password" required minLength={8} autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="p-conf">Confirmar nova senha</Label>
              <Input id="p-conf" type="password" required minLength={8} autoComplete="new-password" value={confirmPw} onChange={(e) => setConfirmPw(e.target.value)} />
            </div>
            <Button type="submit">Alterar senha</Button>
          </form>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Autenticação em dois fatores</CardTitle>
          <CardDescription>
            {user.totp_enabled ? "2FA ativo." : "2FA desativado — recomendado para todos e obrigatório para contas reais."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild variant="outline">
            <Link href="/dashboard/security">Gerenciar 2FA</Link>
          </Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Sessões</CardTitle>
          <CardDescription>Revoga todos os tokens emitidos, inclusive o desta sessão.</CardDescription>
        </CardHeader>
        <CardContent>
          <Button
            variant="destructive"
            onClick={async () => {
              if (!confirm("Encerrar todas as sessões?")) return;
              await api.logoutAll().catch(() => undefined);
              setActiveWorkspace(null);
              router.replace("/auth/login");
            }}
          >
            Sair de todos os dispositivos
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
