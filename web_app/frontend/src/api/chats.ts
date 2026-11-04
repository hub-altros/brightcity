import type {
  ChatDetail,
  ChatListResp,
  ReplyResp,
  EditResp,
} from "../types/api";

const API_URL = import.meta.env.VITE_API_URL || "";
const BASE = `${API_URL}/api`;

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

export const chatsApi = {
  list: (params: { tab: "active" | "archive"; page?: number; q?: string }) => {
    const sp = new URLSearchParams();
    sp.set("tab", params.tab);
    if (params.page) sp.set("page", String(params.page));
    if (params.q) sp.set("q", params.q);
    return request<ChatListResp>(`/chats?${sp.toString()}`);
  },

  get: (platform: string, userId: number) =>
    request<ChatDetail>(`/chats/${platform}/${userId}`),

  reply: async (
    platform: string,
    userId: number,
    text: string,
    replyToId?: number | null,
  ) => {
    return request<ReplyResp>(`/chats/${platform}/${userId}/reply`, {
      method: "POST",
      body: JSON.stringify({
        text,
        reply_to_id: replyToId ?? null,
      }),
    });
  },

  editMessage: async (
    platform: string,
    userId: number,
    messageId: number,
    text: string,
  ) => {
    return request<EditResp>(
      `/chats/${platform}/${userId}/messages/${messageId}`,
      {
        method: "PATCH",
        body: JSON.stringify({ text }),
      },
    );
  },

  archive: (platform: string, userId: number) =>
    request<{ ok: boolean }>(`/chats/${platform}/${userId}/archive`, {
      method: "POST",
    }),

  unarchive: (platform: string, userId: number) =>
    request<{ ok: boolean }>(`/chats/${platform}/${userId}/unarchive`, {
      method: "POST",
    }),
};