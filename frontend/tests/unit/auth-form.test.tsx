import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthForm } from "@/app/auth/auth-form";
import { api } from "@/lib/api";
import type { TokenResponse, User } from "@/lib/types";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: push }),
  useSearchParams: () => new URLSearchParams("next=/dashboard/pnl"),
}));

const user: User = {
  id: "u1",
  email: "ana@exemplo.com",
  full_name: "Ana",
  role: "trader",
  is_active: true,
  active_workspace_id: "ws1",
  created_at: "2026-10-22T00:00:00Z",
  totp_enabled: true,
  mfa_required: false,
  mfa_reason: null,
  disclaimer_pending: false,
} as User;
const token = { access_token: "a", token_type: "bearer", expires_in: 900, user } as TokenResponse;

beforeEach(() => push.mockReset());

describe("AuthForm", () => {
  it("asks for the TOTP code after the password and then follows ?next=", async () => {
    vi.spyOn(api, "login").mockResolvedValue({ mfa_required: true, mfa_token: "mfa-jwt", expires_in: 300 } as never);
    const login2fa = vi.spyOn(api, "login2fa").mockResolvedValue(token);
    render(<AuthForm mode="login" />);
    const u = userEvent.setup();
    await u.type(screen.getByLabelText("E-mail"), "ana@exemplo.com");
    await u.type(screen.getByLabelText("Senha"), "segredo-123");
    await u.click(screen.getByRole("button", { name: "Entrar" }));

    expect(await screen.findByRole("heading", { name: "Verificação em duas etapas" })).toBeInTheDocument();
    const verify = screen.getByRole("button", { name: "Verificar" });
    expect(verify).toBeDisabled();
    await u.type(screen.getByLabelText("Código 2FA"), "123456");
    await u.click(verify);
    expect(login2fa).toHaveBeenCalledWith("mfa-jwt", "123456");
    expect(push).toHaveBeenCalledWith("/dashboard/pnl");
    expect(window.localStorage.getItem("btp.workspace")).toBe("ws1");
  });

  it("switches to recovery codes and back to the password step", async () => {
    vi.spyOn(api, "login").mockResolvedValue({ mfa_required: true, mfa_token: "t", expires_in: 300 } as never);
    render(<AuthForm mode="login" />);
    const u = userEvent.setup();
    await u.type(screen.getByLabelText("E-mail"), "ana@exemplo.com");
    await u.type(screen.getByLabelText("Senha"), "segredo-123");
    await u.click(screen.getByRole("button", { name: "Entrar" }));
    await u.click(await screen.findByRole("button", { name: "Usar código de recuperação" }));
    expect(screen.getByLabelText("Código de recuperação")).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: "Voltar" }));
    expect(screen.getByRole("heading", { name: "Entrar" })).toBeInTheDocument();
  });

  it("registration signs in directly (no second login call)", async () => {
    const register = vi.spyOn(api, "register").mockResolvedValue({ ...token, user: { ...user, totp_enabled: false } });
    const login = vi.spyOn(api, "login");
    render(<AuthForm mode="register" />);
    const u = userEvent.setup();
    await u.type(screen.getByLabelText("E-mail"), "ana@exemplo.com");
    await u.type(screen.getByLabelText("Senha"), "segredo-123");
    await u.click(screen.getByRole("button", { name: /criar conta/i }));
    expect(register).toHaveBeenCalled();
    expect(login).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith("/dashboard/pnl");
  });

  it("sends users who must enrol in 2FA to the security page", async () => {
    vi.spyOn(api, "login").mockResolvedValue({
      ...token,
      user: { ...user, totp_enabled: false, mfa_required: true, mfa_reason: "live" },
    });
    render(<AuthForm mode="login" />);
    const u = userEvent.setup();
    await u.type(screen.getByLabelText("E-mail"), "ana@exemplo.com");
    await u.type(screen.getByLabelText("Senha"), "segredo-123");
    await u.click(screen.getByRole("button", { name: "Entrar" }));
    expect(push).toHaveBeenCalledWith("/dashboard/security?obrigatorio=1");
  });
});
