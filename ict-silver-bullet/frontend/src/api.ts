import type { DashboardSnapshot } from "./types";
export interface Session {
  username: string;
  expires_at: string;
}
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    const message =
      response.status === 401
        ? "Your session has expired. Sign in again."
        : response.status === 429
          ? "Too many attempts. Please wait five minutes."
          : "Request failed. Please retry.";
    throw new ApiError(response.status, message);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export async function dashboard(
  signal?: AbortSignal,
): Promise<DashboardSnapshot> {
  const result = await request<DashboardSnapshot>("/dashboard", { signal });
  if (result.schema_version !== 1)
    throw new Error("Dashboard version mismatch. Reload this page.");
  return result;
}
export const number = (value: number | null | undefined, digits = 2) =>
  value == null
    ? "Unavailable"
    : value.toLocaleString("en-US", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });
export const time = (value: string | null | undefined) =>
  value
    ? new Date(value).toLocaleString("en-GB", {
        timeZone: "UTC",
        hour12: false,
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }) + " UTC"
    : "Unavailable";
export const age = (value: string | null | undefined, now: number) =>
  value ? Math.max(0, Math.floor((now - Date.parse(value)) / 1000)) : null;
export const countdown = (target: string, now: number) => {
  const seconds = Math.max(0, Math.ceil((Date.parse(target) - now) / 1000));
  return `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
};
