import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
it("submits login without storing credentials or browser tokens", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce({ ok: false, status: 401 })
    .mockResolvedValueOnce({ ok: false, status: 401 });
  vi.stubGlobal("fetch", fetch);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <App />
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Username"), "operator");
  await user.type(screen.getByLabelText("Password"), "invalid");
  await user.click(screen.getByRole("button", { name: "Sign in →" }));
  expect(await screen.findByRole("alert")).toHaveProperty(
    "textContent",
    "Invalid username or password.",
  );
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
    username: "operator",
    password: "invalid",
  });
  expect(localStorage.length).toBe(0);
});
it("offers retry when checking the session fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  render(
    <QueryClientProvider client={new QueryClient()}>
      <App />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("button", { name: "Retry" })).toBeTruthy();
});
