import { useQuery } from "@tanstack/react-query";
import { statsApi } from "../api/stats";

export default function Stats() {
  const { data, isLoading } = useQuery({
    queryKey: ["stats"],
    queryFn: statsApi.get,
  });

  if (isLoading || !data) return <div className="p-6 text-slate-500">Загрузка…</div>;

  return (
    <div className="max-w-7xl mx-auto p-4 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight mb-6">Статистика</h1>
      <div className="bg-white rounded-2xl border border-slate-200 p-4 md:p-6 mb-4">
        <div className="text-sm text-slate-500 mb-1">Всего заявок</div>
        <div className="text-3xl font-bold">{data.total}</div>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-white rounded-2xl border border-slate-200 p-4 md:p-6">
          <h2 className="font-bold mb-4">По статусам</h2>
          <div className="space-y-3">
            {data.by_status.map((s) => (
              <div key={s.status}>
                <div className="flex justify-between text-sm mb-1.5">
                  <span className="font-medium">{s.label}</span>
                  <span className="text-slate-500">{s.count}</span>
                </div>
                <div className="h-2 bg-slate-100 rounded-full overflow-hidden">
                  <div className="h-full bg-brand-500 rounded-full"
                    style={{ width: `${data.total ? (s.count * 100) / data.total : 0}%` }} />
                </div>
              </div>
            ))}
          </div>
        </div>
        <div className="bg-white rounded-2xl border border-slate-200 p-4 md:p-6">
          <h2 className="font-bold mb-4">Топ городов</h2>
          <div className="space-y-2 text-sm">
            {data.by_city.map((c) => (
              <div key={c.city} className="flex justify-between">
                <span>{c.city}</span>
                <span className="text-slate-500">{c.count}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}