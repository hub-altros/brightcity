import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { bansApi } from '../api/bans';
import type { Platform } from '../types';

const PLATFORMS: Platform[] = ['vk', 'tg', 'max'];

export function BansPage() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<Platform | ''>('');
  const [form, setForm] = useState({ platform: 'vk' as Platform, user_id: '', reason: '' });
  const [error, setError] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ['bans', filter],
    queryFn: () => bansApi.list(filter || undefined),
  });

  const banMut = useMutation({
    mutationFn: bansApi.create,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bans'] });
      setForm({ platform: 'vk', user_id: '', reason: '' });
      setError(null);
    },
    onError: (e: any) => setError(e?.message || 'Не удалось забанить'),
  });

  const unbanMut = useMutation({
    mutationFn: ({ platform, userId }: { platform: Platform; userId: number }) =>
      bansApi.unban(platform, userId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['bans'] }),
    onError: (e: any) => setError(e?.message || 'Не удалось разбанить'),
  });

  const onCreate = (e: React.FormEvent) => {
    e.preventDefault();
    const uid = parseInt(form.user_id, 10);
    if (!uid || uid <= 0) {
      setError('User ID должен быть положительным числом');
      return;
    }
    banMut.mutate({ platform: form.platform, user_id: uid, reason: form.reason });
  };

  return (
    <div className="max-w-7xl mx-auto p-4 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight mb-6">Забаненные пользователи</h1>

      {/* Форма бана */}
      <form onSubmit={onCreate} className="bg-white rounded-xl border border-slate-200 p-4 mb-4">
        <div className="flex flex-wrap gap-3 items-center">
          <select
            value={form.platform}
            onChange={e => setForm({ ...form, platform: e.target.value as Platform })}
            className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
          >
            {PLATFORMS.map(p => <option key={p} value={p}>{p.toUpperCase()}</option>)}
          </select>
          <input
            value={form.user_id}
            onChange={e => setForm({ ...form, user_id: e.target.value.replace(/\D/g, '') })}
            placeholder="User ID"
            className="border border-slate-300 rounded-lg px-3 py-2 text-sm w-40"
            required
          />
          <input
            value={form.reason}
            onChange={e => setForm({ ...form, reason: e.target.value })}
            placeholder="Причина (необязательно)"
            className="flex-1 min-w-[200px] border border-slate-300 rounded-lg px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={banMut.isPending}
            className="px-4 py-2 rounded-lg bg-red-600 text-white text-sm font-medium disabled:opacity-60"
          >
            {banMut.isPending ? 'Забанить…' : 'Забанить'}
          </button>
        </div>
        {error && (
          <div className="mt-2 text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
            {error}
          </div>
        )}
      </form>

      {/* Фильтры */}
      <div className="flex gap-2 mb-4">
        <button
          onClick={() => setFilter('')}
          className={`px-3.5 py-1.5 rounded-full text-sm ${
            !filter ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-700'
          }`}
        >
          Все
        </button>
        {PLATFORMS.map(p => (
          <button
            key={p}
            onClick={() => setFilter(p)}
            className={`px-3.5 py-1.5 rounded-full text-sm ${
              filter === p ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-700'
            }`}
          >
            {p.toUpperCase()}
          </button>
        ))}
      </div>

      {/* Таблица */}
      {isLoading && <div className="text-slate-500">Загрузка…</div>}
      {data && data.items.length === 0 && (
        <div className="bg-white rounded-2xl border border-slate-200 p-6 text-sm text-slate-500">
          Активных банов нет.
        </div>
      )}
      {data && data.items.length > 0 && (
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-500 text-xs uppercase tracking-wider">
              <tr>
                <th className="text-left px-4 py-3">Пользователь</th>
                <th className="text-left px-4 py-3">Забанен</th>
                <th className="text-left px-4 py-3">До</th>
                <th className="text-left px-4 py-3">Причина</th>
                <th className="text-right px-4 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.items.map(it => (
                <tr key={`${it.platform}:${it.user_id}`} className="hover:bg-slate-50">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium bg-slate-100 text-slate-600">
                        {it.platform.toUpperCase()}
                      </span>
                      <span className="font-mono text-xs">{it.user_id}</span>
                    </div>
                  </td>
                  <td className="px-4 py-3 text-slate-500">
                    {new Date(it.banned_at).toLocaleString('ru', { dateStyle: 'short', timeStyle: 'short' })}
                  </td>
                  <td className="px-4 py-3 text-slate-500">
                    {it.expires_at
                      ? new Date(it.expires_at).toLocaleString('ru', { dateStyle: 'short', timeStyle: 'short' })
                      : 'Навсегда'}
                  </td>
                  <td className="px-4 py-3 text-slate-600">{it.reason || '—'}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => unbanMut.mutate({ platform: it.platform, userId: it.user_id })}
                      disabled={unbanMut.isPending}
                      className="text-emerald-600 hover:text-emerald-800 font-medium text-sm disabled:opacity-60"
                    >
                      Разбанить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}