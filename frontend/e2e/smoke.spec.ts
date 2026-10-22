import { expect, test } from "@playwright/test";

import { nextStep, totp } from "./totp";

const PASSWORD = "Senha-E2E-2026!";

test("cadastro → aviso → 2FA → login em duas etapas → dashboard → tempo real", async ({ page }, testInfo) => {
  const email = `e2e-${testInfo.project.name}-${Date.now()}@exemplo.com.br`;

  // 1. Cadastro já entra na conta (U-10) e o aviso regulatório bloqueia até o aceite.
  await page.goto("/auth/register");
  await expect(page.getByRole("heading", { name: "Criar conta" })).toBeVisible();
  await page.getByLabel("E-mail").fill(email);
  await page.getByLabel("Senha").fill(PASSWORD);
  await page.getByRole("button", { name: /criar conta/i }).click();
  const gate = page.getByRole("dialog", { name: /aviso regulatório/i });
  await expect(gate).toBeVisible();
  await gate.getByRole("checkbox").check();
  await gate.getByRole("button", { name: "Concordo e continuar" }).click();
  await expect(gate).toBeHidden();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  // 2. Ativa o 2FA com o segredo exibido (o teste calcula o código como um app autenticador).
  await page.goto("/dashboard/security");
  await page.getByRole("button", { name: "Configurar 2FA" }).click();
  const secret = (await page.getByLabel("Chave secreta").innerText()).replace(/\s/g, "");
  expect(secret).toMatch(/^[A-Z2-7]{16,}$/);
  await page.getByLabel("Código do aplicativo").fill(totp(secret));
  await page.getByRole("button", { name: "Ativar 2FA" }).click();
  const codes = page.getByRole("list", { name: "Códigos de recuperação" });
  await expect(codes.getByRole("listitem")).toHaveCount(10);
  await page.getByLabel("Guardei os códigos de recuperação").check();
  await page.getByRole("button", { name: "Concluir" }).click();
  await expect(page.getByText(/2FA ativo/)).toBeVisible();

  // 3. Sai e entra de novo: senha e depois o código de 6 dígitos.
  await page.request.post("/api/v1/auth/logout");
  await page.context().clearCookies();
  await page.goto("/auth/login");
  await page.getByLabel("E-mail").fill(email);
  await page.getByLabel("Senha").fill(PASSWORD);
  await page.getByRole("button", { name: "Entrar" }).click();
  await expect(page.getByRole("heading", { name: "Verificação em duas etapas" })).toBeVisible();
  const step = await nextStep(); // o código usado na ativação não pode ser reutilizado
  await page.getByLabel("Código 2FA").fill(totp(secret, step));
  await page.getByRole("button", { name: "Verificar" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  // 4. Conta paper + painel Tempo real.
  const workspaceId = await page.evaluate(() => window.localStorage.getItem("btp.workspace"));
  const created = await page.request.post("/api/v1/accounts/", {
    headers: { "X-Workspace-ID": workspaceId ?? "" },
    data: {
      label: "Paper E2E",
      api_key: "e2e-key-123456",
      api_secret: "e2e-secret-123456",
      is_testnet: true,
      execution_mode: "paper",
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  await page.goto("/dashboard/live");
  await expect(page.getByRole("heading", { name: "Tempo real & Trading" })).toBeVisible();
  await expect(page.getByLabel("Conta")).toContainText("Paper E2E");
});

test("rotas inexistentes mostram a página 404 em português", async ({ page }) => {
  const res = await page.goto("/rota-que-nao-existe");
  expect(res?.status()).toBe(404);
  await expect(page.getByRole("heading", { name: "Página não encontrada" })).toBeVisible();
});
