import { api } from "./client";
import type { GdprItem } from "../types/api";

export const gdprApi = {
  list: () => api<GdprItem[]>("/api/gdpr"),
  del: (id: number) => api<{ ok: true }>(`/api/gdpr/${id}/delete`, { method: "POST" }),
  anon: (id: number) => api<{ ok: true }>(`/api/gdpr/${id}/anon`, { method: "POST" }),
};