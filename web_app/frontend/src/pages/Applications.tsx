import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { appsApi } from "../api/applications";
import { StatusBadge } from "../components/StatusBadge";
import { useLogout } from "../hooks/useAdmin";

const STATUSES: { value: string; label: string }[] = [
  { value: "", label: "Все" },
  { value: "new", label: "Новые" },
  { value: "in_progress", label: "В работе" },
  { value: "rejected", label: "Отклонены" },
  { value: "done", label: "Завершены" },
];

type Sortable = { key: string; label: string; defaultDir: "asc" | "desc" };
const SORT_COLUMNS: Sortable[] = [
  { key: "id",         label: "#",          defaultDir: "asc" },
  { key: "created_at", label: "Создана",    defaultDir: "asc" },
  { key: "name_age",   label: "Ребёнок",    defaultDir: "asc" },
  { key: "user_type",  label: "Тип",        defaultDir: "asc" },
  { key: "status",     label: "Статус",     defaultDir: "asc" },
  { key: "platform",   label: "Мессенджер", defaultDir: "asc" },
];

const SEARCH_DEBOUNCE_MS = 400;
const SORT_STORAGE_KEY = "yg_admin_sort_v1";
const STATUS_STORAGE_KEY = "yg_admin_status_v1";

type SortState = { sort: string; dir: "asc" | "desc" };
const _DEFAULT_SORT: SortState = { sort: "created_at", dir: "asc" };
const _VALID_SORTS = new Set<string>([
  "id", "created_at", "name_age", "user_type", "status", "platform",
]);
const _VALID_STATUSES = new Set<string>(STATUSES.map((s) => s.value));

function _loadSortFromStorage(): SortState {
  try {
    const raw = localStorage.getItem(SORT_STORAGE_KEY);
    if (!raw) return _DEFAULT_SORT;
    const parsed = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed.sort === "string" &&
      _VALID_SORTS.has(parsed.sort) &&
      (parsed.dir === "asc" || parsed.dir === "desc")
    ) {
      return { sort: parsed.sort, dir: parsed.dir };
    }
  } catch {}
  return _DEFAULT_SORT;
}

function _loadStatusFromStorage(): string {
  try {
    const saved = localStorage.getItem(STATUS_STORAGE_KEY);
    if (saved !== null && _VALID_STATUSES.has(saved)) return saved;
  } catch {}
  return "";
}

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 0) return "только что";
  const min = Math.floor(diff / 60_000);
  if (min < 1) return "только что";
  if (min < 60) return `${min} мин назад`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} ч назад`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d} дн назад`;
  return new Date(iso).toLocaleDateString("ru-RU");
}

function PlatformBadge({ platform }: { platform: string | null }) {
  if (!platform) return <span className="text-slate-300 text-xs">—</span>;
  const p = platform.toLowerCase();
  const config: Record<string, { label: string; cls: string }> = {
    vk: { label: "VK", cls: "bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border border-blue-200 dark:border-blue-800/50" },
    tg: { label: "TG", cls: "bg-sky-50 dark:bg-sky-900/30 text-sky-700 dark:text-sky-300 border border-sky-200 dark:border-sky-800/50" },
    max: { label: "MAX", cls: "bg-purple-50 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300 border border-purple-200 dark:border-purple-800/50" },
  };
  const cfg = config[p] || {
    label: p.toUpperCase(),
    cls: "bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-700",
  };
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold tracking-wide ${cfg.cls}`}>
      {cfg.label}
    </span>
  );
}

export default function Applications() {
  const [sp, setSp] = useSearchParams();
  const navigate = useNavigate();
  const doLogout = useLogout();
  const queryClient = useQueryClient();

  const [activeTab, setActiveTab] = useState<"active" | "incomplete">("active");
  const status = sp.get("status") || "";
  const q = sp.get("q") || "";

  const [sortState, setSortState] = useState<SortState>(_loadSortFromStorage);
  const savedStatus = useMemo(() => _loadStatusFromStorage(), []);

  // ── Автообновление списка (15 секунд) ─────────────────────
  const [autoRefresh, setAutoRefresh] = useState(true);

  useEffect(() => {
    if (sp.has("status")) return;
    if (!savedStatus) return;
    if (!_VALID_STATUSES.has(savedStatus)) return;
    const next = new URLSearchParams(sp);
    next.set("status", savedStatus);
    setSp(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    try { localStorage.setItem(STATUS_STORAGE_KEY, status); } catch {}
  }, [status]);

  useEffect(() => {
    try { localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify(sortState)); } catch {}
  }, [sortState]);

  const [qInput, setQInput] = useState(q);
  useEffect(() => { setQInput(q); }, [q]);

  // ── Запрос: Оформленные ─────────────────────────────────────
  const query = useInfiniteQuery({
    queryKey: ["applications", { status, q, sort: sortState.sort, dir: sortState.dir }],
    queryFn: ({ pageParam }) =>
      appsApi.list({ status, q, page: pageParam, sort: sortState.sort, dir: sortState.dir }),
    initialPageParam: 1,
    getNextPageParam: (lastPage) =>
      lastPage.page < lastPage.total_pages ? lastPage.page + 1 : undefined,
    enabled: activeTab === "active",
    refetchInterval: autoRefresh && activeTab === "active" ? 15000 : false,
  });

  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  const total = query.data?.pages[0]?.total ?? 0;

  // ── Запрос: Незавершённые ───────────────────────────────────
  const { data: incompleteData, isLoading: incompleteLoading, isFetching: isIncompleteFetching } = useQuery({
    queryKey: ["applications-incomplete"],
    queryFn: () => appsApi.listIncomplete(),
    enabled: activeTab === "incomplete",
    refetchInterval: autoRefresh && activeTab === "incomplete" ? 15000 : false,
  });

  const remindMutation = useMutation({
    mutationFn: ({ platform, userId }: { platform: string; userId: number }) =>
      appsApi.remindIncomplete(platform, userId),
    onSuccess: () => {
      alert("Напоминание успешно отправлено!");
      queryClient.invalidateQueries({ queryKey: ["applications-incomplete"] });
    },
    onError: (e: any) => {
      alert(`Не удалось отправить напоминание: ${e.message || "Ошибка"}`);
    },
  });

  const setParams = (patch: Record<string, string>) => {
    setSp((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v) next.set(k, v);
        else next.delete(k);
      }
      return next;
    });
  };

  useEffect(() => {
    const timer = setTimeout(() => {
      const trimmed = qInput.trim();
      if (trimmed !== q) setParams({ q: trimmed });
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qInput]);

  const openApp = (id: number) => {
    const qs = sp.toString();
    navigate(`/applications/${id}${qs ? "?" + qs : ""}`);
  };

  const toggleSort = (col: Sortable) => {
    setSortState((prev) => {
      const newDir = prev.sort === col.key ? (prev.dir === "asc" ? "desc" : "asc") : col.defaultDir;
      return { sort: col.key, dir: newDir };
    });
  };

  const sortIndicator = (key: string) =>
    sortState.sort === key ? (sortState.dir === "asc" ? " ↑" : " ↓") : "";

  const loadMoreRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = loadMoreRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && query.hasNextPage && !query.isFetchingNextPage) {
          query.fetchNextPage();
        }
      },
      { rootMargin: "400px" },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [query.hasNextPage, query.isFetchingNextPage, query.fetchNextPage]);

  const bottomStatus = (() => {
    if (query.isLoading) return null;
    if (query.isFetchingNextPage) {
      return (
        <div className="flex items-center justify-center gap-2 py-6 text-slate-500 dark:text-slate-400 text-sm">
          <span className="w-4 h-4 border-2 border-slate-300 border-t-brand-500 rounded-full animate-spin" />
          Загрузка…
        </div>
      );
    }
    if (!query.hasNextPage && items.length > 0) {
      return (
        <div className="text-center py-6 text-xs text-slate-400 dark:text-slate-500">
          Это все заявки · {total}
        </div>
      );
    }
    return null;
  })();

  const renderTabs = (mobile = false) => (
    <div className={`flex gap-2 ${mobile ? "mx-4 mb-2" : ""}`}>
      <button
        onClick={() => setActiveTab("active")}
        className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
          activeTab === "active"
            ? "bg-brand-500 text-white shadow-sm"
            : "bg-slate-100 hover:bg-slate-200 text-slate-600 dark:bg-slate-800 dark:hover:bg-slate-700 dark:text-slate-300"
        }`}
      >
        Оформленные
      </button>
      <button
        onClick={() => setActiveTab("incomplete")}
        className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
          activeTab === "incomplete"
            ? "bg-brand-500 text-white shadow-sm"
            : "bg-slate-100 hover:bg-slate-200 text-slate-600 dark:bg-slate-800 dark:hover:bg-slate-700 dark:text-slate-300"
        }`}
      >
        Незавершённые
      </button>
    </div>
  );

  const toggleAutoRefresh = () => setAutoRefresh((v) => !v);

  const renderAutoRefreshControl = (mobile = false) => (
    <div className="flex items-center gap-1.5">
      <span className={`${mobile ? "text-[11px]" : "text-xs"} text-slate-500 dark:text-slate-400`}>
        Автообновление
      </span>
      <button
        onClick={toggleAutoRefresh}
        title={autoRefresh ? "Автообновление включено (15 сек)" : "Включить автообновление"}
        className={`${mobile ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-xs"} rounded font-semibold transition flex items-center gap-1.5 ${
          autoRefresh
            ? "bg-brand-500 text-white"
            : "bg-slate-200 dark:bg-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-300 dark:hover:bg-slate-600"
        }`}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full ${
            autoRefresh ? "bg-green-400 animate-pulse" : "bg-red-500"
          }`}
        />
        {autoRefresh ? "ВКЛ" : "ВЫКЛ"}
      </button>
    </div>
  );

  return (
    <>
      <header className="md:hidden sticky top-0 z-20 bg-white/95 dark:bg-slate-900/95 backdrop-blur border-b border-slate-200 dark:border-slate-800 safe-top">
        <div className="flex items-center gap-2 px-4 py-3">
          <div className="text-xl font-bold text-slate-900 dark:text-slate-100">Заявки</div>
          {renderAutoRefreshControl(true)}
          <button onClick={doLogout} className="ml-auto text-xs text-slate-500 dark:text-slate-400">
            Выйти
          </button>
        </div>

        {renderTabs(true)}

        {activeTab === "active" && (
          <>
            <div className="px-4 pb-2 relative">
              <input
                value={qInput}
                onChange={(e) => setQInput(e.target.value)}
                placeholder="ФИО, телефон, город, вк / тг / max…"
                className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 pr-9 text-sm bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500"
              />
              {qInput && (
                <button
                  type="button"
                  onClick={() => { setQInput(""); setParams({ q: "" }); }}
                  className="absolute right-5 top-1/2 -translate-y-1/2 w-7 h-7 flex items-center justify-center text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
                >
                  ×
                </button>
              )}
            </div>

            <div className="chip-scroll flex gap-2 px-4 pb-2">
              {STATUSES.map((s) => (
                <button
                  key={s.value}
                  type="button"
                  onClick={() => setParams({ status: s.value })}
                  className={`whitespace-nowrap px-3.5 py-1.5 rounded-full text-sm transition ${
                    s.value === status ? "bg-slate-900 dark:bg-slate-100 text-white dark:text-slate-900" : "bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 active:bg-slate-200 dark:active:bg-slate-700"
                  }`}
                >
                  {s.label}
                </button>
              ))}
            </div>

            <div className="px-4 pb-3 flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
              <span>Сортировка:</span>
              <select
                value={`${sortState.sort}:${sortState.dir}`}
                onChange={(e) => {
                  const [newSort, newDir] = e.target.value.split(":");
                  setSortState({ sort: newSort, dir: newDir as "asc" | "desc" });
                }}
                className="border border-slate-300 dark:border-slate-600 rounded-md px-2 py-1 text-xs bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100"
              >
                <option value="created_at:asc">Сначала старые</option>
                <option value="created_at:desc">Сначала новые</option>
                <option value="id:asc"># ↑</option>
                <option value="id:desc"># ↓</option>
                <option value="name_age:asc">Ребёнок А→Я</option>
                <option value="name_age:desc">Ребёнок Я→А</option>
                <option value="status:asc">Статус А→Я</option>
                <option value="user_type:asc">Тип А→Я</option>
                <option value="platform:asc">Мессенджер А→Я</option>
                <option value="platform:desc">Мессенджер Я→А</option>
              </select>
            </div>
          </>
        )}
      </header>

      <div className="max-w-7xl mx-auto p-4 md:p-6">
        <div className="hidden md:flex flex-col sm:flex-row sm:items-center justify-between mb-4 gap-4">
          <div className="flex items-center gap-4 flex-wrap">
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-slate-900 dark:text-slate-100">Заявки</h1>
              <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
                {activeTab === "active" ? `Всего: ${total}` : `Незавершённых: ${incompleteData?.total || 0}`}
                {activeTab === "active" && query.isFetching && !query.isFetchingNextPage && " · обновление…"}
              </p>
            </div>
            {renderAutoRefreshControl(false)}
          </div>
          <div className="flex items-center gap-3">
            {renderTabs(false)}
          </div>
        </div>

        {/* ── ВКЛАДКА: ОФОРМЛЕННЫЕ ───────────────────────────── */}
        {activeTab === "active" && (
          <>
            <div className="hidden md:flex flex-wrap items-center gap-3 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-4 mb-4">
              <div className="relative flex-1 min-w-[240px]">
                <input
                  value={qInput}
                  onChange={(e) => setQInput(e.target.value)}
                  placeholder="Поиск: ФИО, телефон, город или мессенджер (вк, тг, max)…"
                  className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 pr-9 text-sm bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500"
                />
                {qInput && (
                  <button
                    type="button"
                    onClick={() => { setQInput(""); setParams({ q: "" }); }}
                    className="absolute right-2 top-1/2 -translate-y-1/2 w-7 h-7 flex items-center justify-center text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
                  >
                    ×
                  </button>
                )}
              </div>
              <select
                value={status}
                onChange={(e) => setParams({ status: e.target.value })}
                className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm cursor-pointer bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100"
              >
                {STATUSES.map((s) => (
                  <option key={s.value} value={s.value}>{s.label}</option>
                ))}
              </select>
            </div>

            {query.isLoading && (
              <div className="text-slate-500 dark:text-slate-400 text-sm py-6 text-center">Загрузка…</div>
            )}

            {!query.isLoading && items.length === 0 && (
              <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-6 text-sm text-slate-500 dark:text-slate-400">
                Ничего не найдено
                {q ? ` по запросу «${q}»` : ""}
                {status ? ` в статусе «${STATUSES.find((s) => s.value === status)?.label}»` : ""}.
              </div>
            )}

            <div className="hidden md:block bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-slate-50 dark:bg-slate-900/50 text-slate-500 dark:text-slate-400 text-xs uppercase tracking-wider sticky top-0 z-10">
                  <tr>
                    {SORT_COLUMNS.map((col) => (
                      <th
                        key={col.key}
                        onClick={() => toggleSort(col)}
                        className={`text-left px-4 py-3 cursor-pointer select-none hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors ${
                          sortState.sort === col.key ? "text-slate-900 dark:text-slate-200" : ""
                        }`}
                      >
                        {col.label}
                        <span className="text-slate-400 dark:text-slate-500">{sortIndicator(col.key)}</span>
                      </th>
                    ))}
                    <th className="w-12"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {items.map((it) => (
                    <tr
                      key={it.id}
                      onClick={() => openApp(it.id)}
                      className="hover:bg-slate-50 dark:hover:bg-slate-800/50 cursor-pointer transition-colors"
                    >
                      <td className="px-4 py-3 font-mono text-xs text-slate-500 dark:text-slate-400">#{it.id}</td>
                      <td className="px-4 py-3 text-slate-500 dark:text-slate-400">
                        {new Date(it.created_at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-3">
                          {it.first_media_id ? (
                            <img src={`/media/${it.first_media_id}`} className="w-9 h-9 rounded-lg thumb bg-slate-100 dark:bg-slate-700" loading="lazy" />
                          ) : (
                            <div className="w-9 h-9 rounded-lg bg-slate-100 dark:bg-slate-700" />
                          )}
                          <div>
                            <div className="font-medium text-slate-900 dark:text-slate-100">{it.name_age || "—"}</div>
                            <div className="text-xs text-slate-500 dark:text-slate-400">{it.phone || "—"}</div>
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-slate-600 dark:text-slate-300">{it.user_type || "—"}</td>
                      <td className="px-4 py-3"><StatusBadge status={it.status} bannerUrl={it.banner_url} /></td>
                      <td className="px-4 py-3"><PlatformBadge platform={it.platform} /></td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={(e) => { e.stopPropagation(); openApp(it.id); }}
                          className="text-brand-600 dark:text-brand-400 hover:text-brand-700"
                        >
                          →
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="md:hidden space-y-2">
              {items.map((it) => (
                <Link key={it.id} to={`/applications/${it.id}${sp.toString() ? "?" + sp.toString() : ""}`} className="block bg-white dark:bg-slate-800 rounded-2xl border border-slate-200 dark:border-slate-700 p-3 active:bg-slate-50 dark:active:bg-slate-700/50">
                  <div className="flex gap-3">
                    {it.first_media_id ? (
                      <img src={`/media/${it.first_media_id}`} className="w-14 h-14 rounded-xl thumb bg-slate-100 dark:bg-slate-700" loading="lazy" />
                    ) : (
                      <div className="w-14 h-14 rounded-xl bg-slate-100 dark:bg-slate-700" />
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 text-[11px] text-slate-400 dark:text-slate-500">
                        <span className="font-mono">#{it.id}</span>
                        <span>·</span>
                        <span>{new Date(it.created_at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</span>
                        {it.platform && <><span>·</span><PlatformBadge platform={it.platform} /></>}
                        <span className="ml-auto"><StatusBadge status={it.status} bannerUrl={it.banner_url} /></span>
                      </div>
                      <div className="font-semibold mt-1 truncate text-slate-900 dark:text-slate-100">{it.name_age || "—"}</div>
                      <div className="text-xs text-slate-500 dark:text-slate-400 mt-0.5 truncate">
                        {it.user_type || "—"}{it.location ? ` · ${it.location}` : ""}
                      </div>
                    </div>
                  </div>
                </Link>
              ))}
            </div>

            <div ref={loadMoreRef} aria-hidden="true" className="h-1" />
            {bottomStatus}
          </>
        )}

        {/* ── ВКЛАДКА: НЕЗАВЕРШЁННЫЕ ─────────────────────────── */}
        {activeTab === "incomplete" && (
          <>
            {incompleteLoading && (
              <div className="text-slate-500 dark:text-slate-400 text-sm py-6 text-center">Загрузка…</div>
            )}

            {!incompleteLoading && incompleteData?.items.length === 0 && (
              <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-6 text-sm text-slate-500 dark:text-slate-400">
                Нет незавершённых заявок.
              </div>
            )}

            {!incompleteLoading && incompleteData && incompleteData.items.length > 0 && (
              <>
                <div className="hidden md:block bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 overflow-hidden">
                  <table className="w-full text-sm">
                    <thead className="bg-slate-50 dark:bg-slate-900/50 text-slate-500 dark:text-slate-400 text-xs uppercase tracking-wider">
                      <tr>
                        <th className="text-left px-4 py-3">Платформа</th>
                        <th className="text-left px-4 py-3">ID</th>
                        <th className="text-left px-4 py-3">Шаг</th>
                        <th className="text-left px-4 py-3">Данные</th>
                        <th className="text-left px-4 py-3">Обновлено</th>
                        <th className="text-left px-4 py-3">Напоминание</th>
                        <th className="text-right px-4 py-3"></th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                      {incompleteData.items.map((it) => (
                        <tr key={`${it.platform}:${it.user_id}`} className="hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors">
                          <td className="px-4 py-3"><PlatformBadge platform={it.platform} /></td>
                          <td className="px-4 py-3 font-mono text-xs text-slate-900 dark:text-slate-100">{it.user_id}</td>
                          <td className="px-4 py-3">
                            <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium bg-amber-50 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300 border border-amber-200 dark:border-amber-800/50">
                              {it.step_label}
                            </span>
                          </td>
                          <td className="px-4 py-3">
                            {it.name_age && <div className="font-medium text-slate-900 dark:text-slate-100">{it.name_age}</div>}
                            {it.phone && <div className="text-xs text-slate-500 dark:text-slate-400">{it.phone}</div>}
                            {!it.name_age && !it.phone && <span className="text-slate-400 dark:text-slate-500">—</span>}
                          </td>
                          <td className="px-4 py-3 text-slate-500 dark:text-slate-400">
                            {new Date(it.updated_at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}
                          </td>
                          <td className="px-4 py-3">
                            {it.last_remind_at ? (
                              <span className="text-xs text-slate-600 dark:text-slate-300 whitespace-nowrap" title={new Date(it.last_remind_at).toLocaleString("ru-RU")}>
                                🔔 {timeAgo(it.last_remind_at)}
                              </span>
                            ) : (
                              <span className="text-xs text-slate-400 dark:text-slate-500">—</span>
                            )}
                          </td>
                          <td className="px-4 py-3 text-right">
                            <button
                              onClick={() => remindMutation.mutate({ platform: it.platform, userId: it.user_id })}
                              disabled={remindMutation.isPending && remindMutation.variables?.userId === it.user_id}
                              className="text-brand-600 hover:text-brand-800 dark:text-brand-400 dark:hover:text-brand-300 font-medium text-sm disabled:opacity-50"
                            >
                              {remindMutation.isPending && remindMutation.variables?.userId === it.user_id ? "Отправка…" : "Напомнить"}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="md:hidden space-y-2">
                  {incompleteData.items.map((it) => (
                    <div key={`${it.platform}:${it.user_id}`} className="bg-white dark:bg-slate-800 rounded-2xl border border-slate-200 dark:border-slate-700 p-3">
                      <div className="flex items-center gap-1.5 text-[11px] text-slate-400 dark:text-slate-500 mb-2">
                        <PlatformBadge platform={it.platform} />
                        <span className="font-mono">{it.user_id}</span>
                        <span className="ml-auto">
                          {new Date(it.updated_at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 mb-2 flex-wrap">
                        <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium bg-amber-50 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300 border border-amber-200 dark:border-amber-800/50">
                          {it.step_label}
                        </span>
                        {it.last_remind_at && (
                          <span className="text-[11px] text-slate-500 dark:text-slate-400" title={new Date(it.last_remind_at).toLocaleString("ru-RU")}>
                            🔔 {timeAgo(it.last_remind_at)}
                          </span>
                        )}
                      </div>
                      <div className="text-sm mb-3">
                        {it.name_age && <div className="font-medium text-slate-900 dark:text-slate-100">{it.name_age}</div>}
                        {it.phone && <div className="text-slate-500 dark:text-slate-400 text-xs">{it.phone}</div>}
                        {!it.name_age && !it.phone && <span className="text-slate-400 dark:text-slate-500">Нет данных</span>}
                      </div>
                      <button
                        onClick={() => remindMutation.mutate({ platform: it.platform, userId: it.user_id })}
                        disabled={remindMutation.isPending && remindMutation.variables?.userId === it.user_id}
                        className="w-full py-2 rounded-lg bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-200 text-sm font-medium active:bg-slate-200 dark:active:bg-slate-600 disabled:opacity-50"
                      >
                        {remindMutation.isPending && remindMutation.variables?.userId === it.user_id ? "Отправка…" : "Напомнить"}
                      </button>
                    </div>
                  ))}
                </div>
              </>
            )}
          </>
        )}
      </div>
    </>
  );
}