import { api } from "./client";
import type { Stats } from "../types/api";


export const statsApi = {
  get: () => api<Stats>("/api/stats"),
};