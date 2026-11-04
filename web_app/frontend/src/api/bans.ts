import { api } from './client'; // у тебя уже есть api-клиент
import type { Platform } from '../types'; // если Platform ещё нет, добавь: export type Platform = 'vk' | 'tg' | 'max';

export interface BanItem {
  platform: Platform;
  user_id: number;
  banned_by: number | null;
  banned_by_platform: string | null;
  banned_at: string;
  expires_at: string | null;
  reason: string | null;
  ban_count: number;
}

export interface BanListResp {
  items: BanItem[];
  total: number;
  limit: number;
  offset: number;
}

export const bansApi = {
  list: (platform?: Platform) => {
    const qs = platform ? `?platform=${platform}` : '';
    return api<BanListResp>(`/api/bans${qs}`);
  },
  create: (data: { platform: Platform; user_id: number; reason?: string }) =>
    api<{ ok: boolean }>('/api/bans', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  unban: (platform: Platform, userId: number) =>
    api<{ ok: boolean }>(`/api/bans/${platform}/${userId}/unban`, {
      method: 'POST',
    }),
};