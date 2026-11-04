import { api } from "./client";
import type { Admin } from "../types/api";

export type TwoFASetup = { secret: string; qr_data_url: string };

export const authApi = {
  me: () => api<Admin>("/api/auth/me"),
  login: (login: string, password: string) =>
    api<{ redirect: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ login, password }),
    }),
  setup2fa: () => api<TwoFASetup>("/api/auth/2fa/setup"),
  confirm2fa: (code: string) =>
    api<{ recovery_codes: string[] }>("/api/auth/2fa/setup", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
  completeSetup: () =>
    api<{ ok: true }>("/api/auth/2fa/setup/complete", { method: "POST" }),
  verify2fa: (code: string, useRecovery = false) =>
    api<{ ok: true }>("/api/auth/2fa/verify", {
      method: "POST",
      body: JSON.stringify({ code, use_recovery: useRecovery }),
    }),
  logout: () => api<{ ok: true }>("/api/auth/logout", { method: "POST" }),
};