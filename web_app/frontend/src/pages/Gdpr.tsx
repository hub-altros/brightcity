import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { gdprApi } from "../api/gdpr";

export default function Gdpr() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["gdpr"],
    queryFn: gdprApi.list,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["gdpr"] });

  const del = useMutation({
    mutationFn: gdprApi.del,
    onSuccess: () => { toast.success("Удалено"); invalidate(); },
    onError: (e: Error) => toast.error(e.message),
  });
  const anon = useMutation({
    mutationFn: gdprApi.anon,
    onSuccess: () => { toast.success("Анонимизировано"); invalidate(); },
    onError: (e: Error) => toast.error(e.message),
  });

  if (isLoading) return <div className="p-6 text-slate-500">Загрузка…</div>;

  return (
    <div className="max-w-4xl mx-auto p-4 md:p-6">
      <h1 className="text-2xl font-bold mb-6">Запросы на удаление ПДн</h1>
      {(!data || data.length === 0) && (
        <div className="bg-white rounded-2xl border border-slate-200 p-6 text-sm text-slate-500">
          Необработанных запросов нет.
        </div>
      )}
      <div className="space-y-3">
        {data?.map((it) => (
          <div key={it.id} className="bg-white rounded-2xl border border-slate-200 p-4">
            <div className="flex items-start gap-3">
              <div className="w-10 h-10 rounded-xl bg-red-50 text-red-600 flex items-center justify-center text-lg">🗑</div>
              <div className="flex-1">
                <div className="font-semibold">Запрос #{it.id}</div>
                <div className="text-sm text-slate-500 mt-0.5">{it.platform.toUpperCase()}:{it.user_id}</div>
                <div className="text-xs text-slate-400 mt-0.5">
                  {new Date(it.requested_at).toLocaleString("ru-RU")}
                </div>
              </div>
            </div>
            <div className="flex gap-2 mt-3">
              <button
                onClick={() => {
                  if (confirm("Удалить данные пользователя? Действие необратимо.")) {
                    del.mutate(it.id);
                  }
                }}
                disabled={del.isPending}
                className="flex-1 px-3 py-2.5 rounded-lg bg-red-50 text-red-700 text-sm font-medium disabled:opacity-50"
              >
                Удалить
              </button>
              <button
                onClick={() => {
                  if (confirm("Анонимизировать данные пользователя?")) {
                    anon.mutate(it.id);
                  }
                }}
                disabled={anon.isPending}
                className="flex-1 px-3 py-2.5 rounded-lg bg-slate-100 text-slate-700 text-sm font-medium disabled:opacity-50"
              >
                Анонимизировать
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}