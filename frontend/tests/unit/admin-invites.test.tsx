import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { InvitesTab, inviteLink } from "@/components/admin-invites";
import { api } from "@/lib/api";
import type { InviteCreated, RegistrationInvite } from "@/lib/types";

const pending: RegistrationInvite = {
  id: "i1",
  email: "bia@exemplo.com",
  note: "mesa 2",
  status: "pending",
  created_at: "2026-10-22T10:00:00Z",
  expires_at: "2026-10-25T10:00:00Z",
  used_at: null,
  used_by_id: null,
};
const used: RegistrationInvite = { ...pending, id: "i2", email: null, note: null, status: "used", used_at: "2026-10-22T11:00:00Z" };

describe("inviteLink", () => {
  it("points to the signup page with the token in ?invite=", () => {
    expect(inviteLink("a b/c", "https://isc.example")).toBe("https://isc.example/auth/register?invite=a%20b%2Fc");
  });
});

describe("InvitesTab", () => {
  it("lists invites, revokes only pending ones and shows a new link once", async () => {
    vi.spyOn(api, "registrationMode").mockResolvedValue({ mode: "invite" });
    const list = vi.spyOn(api, "adminInvites").mockResolvedValue([pending, used]);
    const created: InviteCreated = { ...pending, id: "i3", email: null, token: "tok123", registration_mode: "invite" };
    const create = vi.spyOn(api, "adminCreateInvite").mockResolvedValue(created);
    const revoke = vi.spyOn(api, "adminRevokeInvite").mockResolvedValue({ ...pending, status: "revoked" });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<InvitesTab />);
    const u = userEvent.setup();
    expect(await screen.findByText("bia@exemplo.com")).toBeInTheDocument();
    expect(screen.getByText("Utilizado")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /revogar convite/i })).toHaveLength(1);

    await u.click(screen.getByRole("button", { name: "Revogar convite bia@exemplo.com" }));
    expect(revoke).toHaveBeenCalledWith("i1");

    await u.type(screen.getByLabelText("Observação (opcional)"), "novo analista");
    await u.click(screen.getByRole("button", { name: "Gerar convite" }));
    expect(create).toHaveBeenCalledWith({ email: undefined, note: "novo analista", hours: 72 });
    const status = await screen.findByRole("status");
    expect(within(status).getByLabelText("Link do convite")).toHaveValue(
      `${window.location.origin}/auth/register?invite=tok123`,
    );
    expect(list.mock.calls.length).toBeGreaterThanOrEqual(3); // initial + after revoke + after create
    await u.click(within(status).getByRole("button", { name: "Fechar" }));
    expect(screen.queryByLabelText("Link do convite")).not.toBeInTheDocument();
  });

  it("warns when signup is closed (invites are not accepted)", async () => {
    vi.spyOn(api, "registrationMode").mockResolvedValue({ mode: "closed" });
    vi.spyOn(api, "adminInvites").mockResolvedValue([]);
    render(<InvitesTab />);
    expect(await screen.findByText(/cadastro está fechado/i)).toBeInTheDocument();
    expect(screen.getByText("Nenhum convite criado.")).toBeInTheDocument();
  });
});
