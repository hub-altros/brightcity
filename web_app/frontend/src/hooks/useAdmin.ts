import { useQuery, useQueryClient } from "@tanstack/react-query";
import { authApi } from "../api/auth";
import type { Admin } from "../types/api";

export function useAdmin() {
  return useQuery<Admin | null>({
    queryKey: ["admin"],
    queryFn: async () => {
      try {
        return await authApi.me();
      } catch {
        return null;
      }
    },
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return async () => {
    await authApi.logout();
    qc.clear();
    window.location.href = "/login";
  };
}