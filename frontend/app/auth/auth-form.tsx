"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { LegalFooter } from "@/components/legal";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorMessage, setActiveWorkspace } from "@/lib/api";
import type { RegistrationMode, TokenResponse } from "@/lib/types";

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [useRecovery, setUseRecovery] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [invite, setInvite] = useState(
    () => params.get("convite") ?? params.get("invite") ?? "",
  );
  // Signup policy from the API (closed / invite / open); null while loading or if the request fails.
  const [signup, setSignup] = useState<RegistrationMode | null>(null);
  const isLogin = mode === "login";
  const closed = !isLogin && signup === "closed";

  useEffect(() => {
    let alive = true;
    api
      .registrationMode()
      .then((r) => alive && setSignup(r.mode))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  function finish(res: TokenResponse) {
    setActiveWorkspace(res.user.active_workspace_id);
    const next = params.get("next");
    // Users that must enrol in 2FA land on Segurança (the API is blocked for them until then).
    if (
      res.user.mfa_required &&
      !res.user.totp_enabled &&
      res.user.mfa_reason !== "admin"
    ) {
      router.push("/dashboard/security?obrigatorio=1");
      return;
    }
    router.push(
      next && next.startsWith("/") && !next.startsWith("//")
        ? next
        : "/dashboard",
    );
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      if (!isLogin) {
        // Registration signs in immediately (same cookies as login): go straight to the dashboard.
        finish(
          await api.register(
            email,
            password,
            fullName || undefined,
            invite.trim() || undefined,
          ),
        );
        return;
      }
      const res = await api.login(email, password);
      if ("mfa_required" in res && res.mfa_required) {
        setMfaToken(res.mfa_token);
        setPassword("");
        return;
      }
      finish(res as TokenResponse);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }

  async function onSubmitCode(e: React.FormEvent) {
    e.preventDefault();
    if (!mfaToken) return;
    setError(null);
    setLoading(true);
    try {
      finish(await api.login2fa(mfaToken, code));
    } catch (err) {
      setError(errorMessage(err));
      if (err instanceof Error && /expirada/i.test(err.message)) {
        setMfaToken(null);
        setCode("");
      }
    } finally {
      setLoading(false);
    }
  }

  const title = mfaToken
    ? "Verificação em duas etapas"
    : isLogin
      ? "Entrar"
      : "Criar conta";

  return (
    <div className="flex min-h-screen flex-col">
      <main
        id="conteudo"
        className="container flex flex-1 items-center justify-center py-8"
      >
        <Card className="w-full max-w-sm">
          <CardHeader>
            <h1 className="text-2xl font-semibold leading-none tracking-tight">
              {title}
            </h1>
            <CardDescription>
              {mfaToken
                ? useRecovery
                  ? "Digite um dos seus códigos de recuperação (cada código funciona uma única vez)."
                  : "Digite o código de 6 dígitos do seu aplicativo autenticador."
                : isLogin
                  ? "Acesse sua plataforma de trading."
                  : signup === "invite"
                    ? "Cadastro somente por convite: use o código enviado pelo administrador."
                    : "Ao concluir o cadastro você já entra na plataforma."}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {mfaToken ? (
              <form onSubmit={onSubmitCode} className="space-y-4" noValidate>
                <div className="space-y-2">
                  <Label htmlFor="mfa-code">
                    {useRecovery ? "Código de recuperação" : "Código 2FA"}
                  </Label>
                  <Input
                    id="mfa-code"
                    required
                    autoFocus
                    autoComplete="one-time-code"
                    inputMode={useRecovery ? "text" : "numeric"}
                    pattern={useRecovery ? undefined : "[0-9 ]*"}
                    maxLength={useRecovery ? 20 : 7}
                    placeholder={useRecovery ? "XXXXX-XXXXX" : "123456"}
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                  />
                </div>
                {error && <Alert variant="destructive">{error}</Alert>}
                <Button
                  type="submit"
                  className="w-full"
                  disabled={loading || code.trim().length < 6}
                >
                  {loading ? "Verificando..." : "Verificar"}
                </Button>
                <div className="flex justify-between text-sm">
                  <button
                    type="button"
                    className="text-muted-foreground underline underline-offset-2 hover:text-foreground"
                    onClick={() => {
                      setUseRecovery((v) => !v);
                      setCode("");
                    }}
                  >
                    {useRecovery
                      ? "Usar o aplicativo autenticador"
                      : "Usar código de recuperação"}
                  </button>
                  <button
                    type="button"
                    className="text-muted-foreground underline underline-offset-2 hover:text-foreground"
                    onClick={() => {
                      setMfaToken(null);
                      setCode("");
                      setError(null);
                    }}
                  >
                    Voltar
                  </button>
                </div>
              </form>
            ) : closed ? (
              <div className="space-y-4">
                <Alert>
                  O cadastro está fechado nesta instalação. Peça ao
                  administrador um convite ou que crie a sua conta.
                </Alert>
                <p className="text-center text-sm text-muted-foreground">
                  Já tem conta?{" "}
                  <Link className="underline" href="/auth/login">
                    Entrar
                  </Link>
                </p>
              </div>
            ) : (
              <form onSubmit={onSubmit} className="space-y-4">
                {!isLogin && (
                  <div className="space-y-2">
                    <Label htmlFor="name">Nome</Label>
                    <Input
                      id="name"
                      value={fullName}
                      onChange={(e) => setFullName(e.target.value)}
                      autoComplete="name"
                    />
                  </div>
                )}
                <div className="space-y-2">
                  <Label htmlFor="email">E-mail</Label>
                  <Input
                    id="email"
                    type="email"
                    required
                    autoComplete="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="password">Senha</Label>
                  <Input
                    id="password"
                    type="password"
                    required
                    minLength={isLogin ? 1 : 8}
                    autoComplete={isLogin ? "current-password" : "new-password"}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    aria-describedby={isLogin ? undefined : "password-hint"}
                  />
                  {!isLogin && (
                    <p
                      id="password-hint"
                      className="text-xs text-muted-foreground"
                    >
                      Mínimo de 8 caracteres.
                    </p>
                  )}
                </div>
                {!isLogin && signup === "invite" && (
                  <div className="space-y-2">
                    <Label htmlFor="invite">Código de convite</Label>
                    <Input
                      id="invite"
                      required
                      autoComplete="off"
                      spellCheck={false}
                      value={invite}
                      onChange={(e) => setInvite(e.target.value)}
                    />
                  </div>
                )}
                {error && <Alert variant="destructive">{error}</Alert>}
                <Button type="submit" className="w-full" disabled={loading}>
                  {loading ? "Aguarde..." : isLogin ? "Entrar" : "Criar conta"}
                </Button>
                <p className="text-center text-sm text-muted-foreground">
                  {isLogin ? (
                    signup !== "closed" && (
                      <>
                        Não tem conta?{" "}
                        <Link className="underline" href="/auth/register">
                          Cadastre-se
                        </Link>
                      </>
                    )
                  ) : (
                    <>
                      Já tem conta?{" "}
                      <Link className="underline" href="/auth/login">
                        Entrar
                      </Link>
                    </>
                  )}
                </p>
              </form>
            )}
          </CardContent>
        </Card>
      </main>
      <LegalFooter />
    </div>
  );
}
