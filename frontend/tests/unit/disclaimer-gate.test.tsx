import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DISCLAIMER_VERSION, DisclaimerGate, LegalFooter } from "@/components/legal";
import { api, ApiError } from "@/lib/api";
import type { User } from "@/lib/types";

const base = { id: "u", email: "a@b.com", role: "trader", disclaimer_pending: true } as User;

describe("DisclaimerGate", () => {
  it("is hidden once accepted", () => {
    const { container } = render(<DisclaimerGate user={{ ...base, disclaimer_pending: false }} onAccepted={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("requires the checkbox and records the current version", async () => {
    const accept = vi.spyOn(api, "acceptDisclaimer").mockResolvedValue({} as never);
    const onAccepted = vi.fn();
    render(<DisclaimerGate user={base} onAccepted={onAccepted} />);
    const dialog = screen.getByRole("dialog", { name: /aviso regulatório/i });
    expect(dialog).toHaveTextContent("BCB nº 519, 520 e 521");
    expect(dialog).toHaveTextContent("CVM");
    const button = screen.getByRole("button", { name: "Concordo e continuar" });
    expect(button).toBeDisabled();
    const u = userEvent.setup();
    await u.click(screen.getByRole("checkbox"));
    await u.click(button);
    expect(accept).toHaveBeenCalledWith(DISCLAIMER_VERSION);
    expect(onAccepted).toHaveBeenCalled();
  });

  it("shows API errors", async () => {
    vi.spyOn(api, "acceptDisclaimer").mockRejectedValue(new ApiError(409, "Versão do aviso desatualizada"));
    render(<DisclaimerGate user={base} onAccepted={vi.fn()} />);
    const u = userEvent.setup();
    await u.click(screen.getByRole("checkbox"));
    await u.click(screen.getByRole("button", { name: "Concordo e continuar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Versão do aviso desatualizada");
  });

  it("footer links to the full notice", () => {
    render(<LegalFooter />);
    expect(screen.getByRole("link", { name: "Aviso completo" })).toHaveAttribute("href", "/legal");
  });
});
