import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, MFA_REQUIRED_EVENT, setActiveWorkspace } from "@/lib/api";

function json(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("sends the active workspace header and cookies", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(200, []));
    vi.stubGlobal("fetch", fetchMock);
    setActiveWorkspace("ws-1");
    await api.listPresets();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/strategies/presets");
    expect(init.credentials).toBe("include");
    expect(new Headers(init.headers).get("X-Workspace-ID")).toBe("ws-1");
  });

  it("refreshes once on 401 and retries the request", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json(401, { detail: "expirado" }))
      .mockResolvedValueOnce(json(200, {})) // /auth/refresh
      .mockResolvedValueOnce(json(200, [{ key: "x" }]));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.listPresets()).resolves.toEqual([{ key: "x" }]);
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/auth/refresh");
  });

  it("does not try to refresh on a failed login and surfaces the Portuguese detail", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(401, { detail: "E-mail ou senha inválidos" }));
    vi.stubGlobal("fetch", fetchMock);
    const err = await api.login("a@b.com", "x").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(401);
    expect(err.message).toBe("E-mail ou senha inválidos");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("joins FastAPI validation errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(422, { detail: [{ msg: "campo a" }, { msg: "campo b" }] })));
    await expect(api.createPreset({} as never)).rejects.toThrow("campo a; campo b");
  });

  it("emits the MFA-required event on 403 + X-MFA-Enrollment-Required", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(json(403, { detail: "2FA obrigatório" }, { "x-mfa-enrollment-required": "live" }))
    );
    const listener = vi.fn();
    window.addEventListener(MFA_REQUIRED_EVENT, listener);
    await expect(api.listAccounts()).rejects.toThrow("2FA obrigatório");
    window.removeEventListener(MFA_REQUIRED_EVENT, listener);
    expect(listener).toHaveBeenCalledTimes(1);
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toBe("live");
  });

  it("returns undefined for 204 responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(204, null)));
    await expect(api.deletePreset("p1")).resolves.toBeUndefined();
  });
});
