import type {
  ApplicationDetail,
  ApplicationsListResp,
  ApplicationsIncompleteResp,
} from "../types/api";

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
    ...init,
  });

  if (!res.ok) {
    let detail = `Ошибка ${res.status}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = j.detail;
    } catch {}
    throw new Error(detail);
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const appsApi = {
  list: (params?: { page?: number; status?: string; q?: string; sort?: string; dir?: string }) => {
    const sp = new URLSearchParams();
    if (params?.page) sp.set("page", String(params.page));
    if (params?.status) sp.set("status", params.status);
    if (params?.q) sp.set("q", params.q);
    if (params?.sort) sp.set("sort", params.sort);
    if (params?.dir) sp.set("dir", params.dir);
    const qs = sp.toString();
    return request<ApplicationsListResp>(`/applications${qs ? "?" + qs : ""}`);
  },

  get: (id: number, params?: Record<string, string>) => {
    const sp = new URLSearchParams();
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v) sp.set(k, v);
      }
    }
    const qs = sp.toString();
    return request<ApplicationDetail>(`/applications/${id}${qs ? "?" + qs : ""}`);
  },

  setInProgress: async (id: number) => {
    const fd = new FormData();
    fd.append("new_status", "in_progress");
    const res = await fetch(`${BASE}/applications/${id}/status`, {
      method: "POST",
      body: fd,
      credentials: "include",
    });
    if (!res.ok) throw new Error(`Ошибка ${res.status}`);
  },

  reject: async (id: number, reason: string) => {
    const fd = new FormData();
    fd.append("reason", reason);
    const res = await fetch(`${BASE}/applications/${id}/reject`, {
      method: "POST",
      body: fd,
      credentials: "include",
    });
    if (!res.ok) throw new Error(`Ошибка ${res.status}`);
  },

  reply: async (id: number, text: string) => {
    const fd = new FormData();
    fd.append("text", text);
    const res = await fetch(`${BASE}/applications/${id}/reply`, {
      method: "POST",
      body: fd,
      credentials: "include",
    });
    if (!res.ok) throw new Error(`Ошибка ${res.status}`);
  },

  uploadBanner: async (id: number, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`${BASE}/applications/${id}/banner`, {
      method: "POST",
      body: fd,
      credentials: "include",
    });
    if (!res.ok) {
      let detail = `Ошибка ${res.status}`;
      try {
        const j = await res.json();
        if (j?.detail) detail = j.detail;
      } catch {}
      throw new Error(detail);
    }
  },

  // ─── НОВЫЕ МЕТОДЫ ДЛЯ ВКЛАДКИ «НЕЗАВЕРШЁННЫЕ» ─────────────────

  listIncomplete: () => 
    request<ApplicationsIncompleteResp>(`/applications/incomplete`),

  remindIncomplete: async (platform: string, userId: number) => {
    // Бэкенд ждёт параметры в пути: /incomplete/{platform}/{user_id}/remind
    const res = await fetch(
      `${BASE}/applications/incomplete/${platform}/${userId}/remind`, 
      {
        method: "POST",
        credentials: "include",
        // FormData больше не нужна, тело запроса пустое
      }
    );
    
    if (!res.ok) {
      let detail = `Ошибка ${res.status}`;
      try {
        const j = await res.json();
        if (j?.detail) detail = j.detail;
      } catch {}
      throw new Error(detail);
    }
  },
};