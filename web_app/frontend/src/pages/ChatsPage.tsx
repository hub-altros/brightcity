import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  useInfiniteQuery,
  useQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { toast } from "sonner";
import { chatsApi } from "../api/chats";
import type { ChatMessage } from "../types/api";

function PlatformBadge({ platform }: { platform: string | null }) {
  if (!platform) return null;
  const p = platform.toLowerCase();
  const cfg: Record<string, { label: string; cls: string }> = {
    vk:  { label: "VK",  cls: "bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border border-blue-200 dark:border-blue-800/50" },
    tg:  { label: "TG",  cls: "bg-sky-50 dark:bg-sky-900/30 text-sky-700 dark:text-sky-300 border border-sky-200 dark:border-sky-800/50" },
    max: { label: "MAX", cls: "bg-purple-50 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300 border border-purple-200 dark:border-purple-800/50" },
  };
  const c = cfg[p] || {
    label: p.toUpperCase(),
    cls: "bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-700",
  };
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold tracking-wide ${c.cls}`}>
      {c.label}
    </span>
  );
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const min = Math.floor(diff / 60_000);
  if (min < 1) return "только что";
  if (min < 60) return `${min} мин`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} ч`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d} дн`;
  return new Date(iso).toLocaleDateString("ru-RU");
}

export default function ChatsPage() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<"active" | "archive">("active");
  const [selected, setSelected] = useState<
    { platform: string; userId: number } | null
  >(null);
  const [search, setSearch] = useState("");
  const [onlyUnanswered, setOnlyUnanswered] = useState(false);

  const [replyText, setReplyText] = useState("");
  const [replyTo, setReplyTo] = useState<ChatMessage | null>(null);

  const [editingMessage, setEditingMessage] = useState<ChatMessage | null>(null);
  const [editText, setEditText] = useState("");

  // ── Список ─────────────────────────────────────────────────
  const listQuery = useInfiniteQuery({
    queryKey: ["chats-list", { tab, q: search }],
    queryFn: ({ pageParam }) =>
      chatsApi.list({
        tab,
        q: search.trim() || undefined,
        page: pageParam,
      }),
    initialPageParam: 1,
    getNextPageParam: (last) =>
      last.page < last.total_pages ? last.page + 1 : undefined,
    refetchInterval: 5000,
  });

  const items = listQuery.data?.pages.flatMap((p) => p.items) ?? [];
  const total = listQuery.data?.pages[0]?.total ?? 0;

  const visibleItems = onlyUnanswered
    ? items.filter((i) => i.is_unanswered)
    : items;

  // Автовыбор первого диалога (десктоп)
  const autoSelectedRef = useRef(false);
  useEffect(() => {
    if (
      !autoSelectedRef.current &&
      !selected &&
      visibleItems.length > 0 &&
      typeof window !== "undefined" &&
      window.innerWidth >= 768
    ) {
      setSelected({
        platform: visibleItems[0].platform,
        userId: visibleItems[0].user_id,
      });
      autoSelectedRef.current = true;
    }
  }, [visibleItems, selected]);

  // ── Детали ─────────────────────────────────────────────────
  const detailQuery = useQuery({
    queryKey: ["chat-detail", selected],
    queryFn: () =>
      selected
        ? chatsApi.get(selected.platform, selected.userId)
        : Promise.reject("no selected"),
    enabled: !!selected,
    refetchInterval: 5000,
  });

  const detail = detailQuery.data;

  // Автоинвалидация списка при новых сообщениях
  const prevMessageCountRef = useRef(0);
  useEffect(() => {
    if (!detail?.messages) return;
    const count = detail.messages.length;
    if (count > prevMessageCountRef.current && prevMessageCountRef.current > 0) {
      qc.invalidateQueries({ queryKey: ["chats-list"] });
    }
    prevMessageCountRef.current = count;
  }, [detail?.messages, qc]);

  // ── Автоскролл ─────────────────────────────────────────────
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const messagesContainerRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const chatKey = selected ? `${selected.platform}:${selected.userId}` : "";

  useEffect(() => {
    if (!chatKey) return;
    shouldAutoScrollRef.current = true;
    setReplyTo(null);
    setEditingMessage(null);
    const t = setTimeout(() => {
      messagesEndRef.current?.scrollIntoView({ behavior: "auto" });
    }, 50);
    return () => clearTimeout(t);
  }, [chatKey]);

  useEffect(() => {
    if (!chatKey) return;
    if (!shouldAutoScrollRef.current) return;
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [detail?.messages.length, chatKey]);

  const handleMessagesScroll = () => {
    const el = messagesContainerRef.current;
    if (!el) return;
    const distanceFromBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight;
    shouldAutoScrollRef.current = distanceFromBottom < 100;
  };

  const scrollToMessage = (messageId: number) => {
    const el = document.getElementById(`msg-${messageId}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add("ring-2", "ring-brand-500");
      setTimeout(() => el.classList.remove("ring-2", "ring-brand-500"), 1200);
    }
  };

  // ── Мутации ─────────────────────────────────────────────────
  const replyMut = useMutation({
    mutationFn: (args: {
      platform: string;
      userId: number;
      text: string;
      replyToId?: number | null;
    }) =>
      chatsApi.reply(args.platform, args.userId, args.text, args.replyToId),
    onSuccess: () => {
      setReplyText("");
      setReplyTo(null);
      toast.success("Отправлено");
      shouldAutoScrollRef.current = true;
      qc.refetchQueries({ queryKey: ["chat-detail", selected] });
      qc.refetchQueries({ queryKey: ["chats-list"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const editMut = useMutation({
    mutationFn: (args: { id: number; text: string }) => {
      if (!selected) throw new Error("Нет выбранного чата");
      return chatsApi.editMessage(
        selected.platform, selected.userId, args.id, args.text,
      );
    },
    onSuccess: (res) => {
      setEditingMessage(null);
      setEditText("");
      toast.success(
        res.mode === "native" ? "Сохранено" : "Добавлено уточнение",
      );
      qc.refetchQueries({ queryKey: ["chat-detail", selected] });
      qc.refetchQueries({ queryKey: ["chats-list"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const archiveMut = useMutation({
    mutationFn: (args: { platform: string; userId: number }) =>
      chatsApi.archive(args.platform, args.userId),
    onMutate: async (vars) => {
      await qc.cancelQueries({ queryKey: ["chats-list"] });
      const prevActive = qc.getQueryData([
        "chats-list", { tab: "active", q: search },
      ]);
      qc.setQueryData<any>(
        ["chats-list", { tab: "active", q: search }],
        (old: any) => {
          if (!old) return old;
          return {
            ...old,
            pages: old.pages.map((p: any) => ({
              ...p,
              items: p.items.filter(
                (i: any) =>
                  !(i.platform === vars.platform && i.user_id === vars.userId),
              ),
              total: Math.max(0, p.total - 1),
            })),
          };
        },
      );
      return { prevActive };
    },
    onError: (e: Error, _vars, ctx) => {
      if (ctx?.prevActive !== undefined) {
        qc.setQueryData(
          ["chats-list", { tab: "active", q: search }],
          ctx.prevActive,
        );
      }
      toast.error(e.message);
    },
    onSuccess: () => {
      toast.success("Перемещено в архив");
      setSelected(null);
      setReplyTo(null);
      setEditingMessage(null);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["chats-list"] });
    },
  });

  const unarchiveMut = useMutation({
    mutationFn: (args: { platform: string; userId: number }) =>
      chatsApi.unarchive(args.platform, args.userId),
    onMutate: async (vars) => {
      await qc.cancelQueries({ queryKey: ["chats-list"] });
      const prevArchive = qc.getQueryData([
        "chats-list", { tab: "archive", q: search },
      ]);
      qc.setQueryData<any>(
        ["chats-list", { tab: "archive", q: search }],
        (old: any) => {
          if (!old) return old;
          return {
            ...old,
            pages: old.pages.map((p: any) => ({
              ...p,
              items: p.items.filter(
                (i: any) =>
                  !(i.platform === vars.platform && i.user_id === vars.userId),
              ),
              total: Math.max(0, p.total - 1),
            })),
          };
        },
      );
      return { prevArchive };
    },
    onError: (e: Error, _vars, ctx) => {
      if (ctx?.prevArchive !== undefined) {
        qc.setQueryData(
          ["chats-list", { tab: "archive", q: search }],
          ctx.prevArchive,
        );
      }
      toast.error(e.message);
    },
    onSuccess: () => {
      toast.success("Возвращено в актуальные");
      setSelected(null);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["chats-list"] });
    },
  });

  const onSubmitReply = (e: React.FormEvent) => {
    e.preventDefault();
    if (!selected || !replyText.trim()) return;
    replyMut.mutate({
      platform: selected.platform,
      userId: selected.userId,
      text: replyText.trim(),
      replyToId: replyTo?.id ?? null,
    });
  };

  const startEdit = (m: ChatMessage) => {
    setEditingMessage(m);
    setEditText(m.text);
    setReplyTo(null);
  };

  const cancelEdit = () => {
    setEditingMessage(null);
    setEditText("");
  };

  const saveEdit = () => {
    if (!editingMessage) return;
    const trimmed = editText.trim();
    if (!trimmed) {
      toast.error("Пустой текст");
      return;
    }
    if (trimmed === editingMessage.text) {
      cancelEdit();
      return;
    }
    editMut.mutate({ id: editingMessage.id, text: trimmed });
  };

  const selectedItem = items.find(
    (i) =>
      i.platform === selected?.platform && i.user_id === selected?.userId,
  );

  // Пагинация списка
  const loadMoreRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = loadMoreRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (entries) => {
        if (
          entries[0].isIntersecting &&
          listQuery.hasNextPage &&
          !listQuery.isFetchingNextPage
        ) {
          listQuery.fetchNextPage();
        }
      },
      { rootMargin: "200px" },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [
    listQuery.hasNextPage,
    listQuery.isFetchingNextPage,
    listQuery.fetchNextPage,
  ]);

  return (
    <div className="h-[calc(100vh-4.5rem)] md:h-[calc(100vh-3rem)] flex border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden bg-white dark:bg-slate-800">
      {/* Список чатов */}
      <aside
        className={`${
          selected ? "hidden md:flex" : "flex"
        } flex-col w-full md:w-96 border-r border-slate-200 dark:border-slate-700`}
      >
        <div className="p-4 border-b border-slate-200 dark:border-slate-700">
          <div className="flex items-center justify-between mb-3">
            <h1 className="text-lg font-bold text-slate-900 dark:text-slate-100">
              Переписка
            </h1>
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {total}
            </span>
          </div>

          <div className="flex gap-1 mb-3">
            <button
              onClick={() => {
                setTab("active");
                setSelected(null);
                setReplyTo(null);
                setEditingMessage(null);
              }}
              className={`flex-1 px-3 py-1.5 rounded-lg text-sm font-medium transition ${
                tab === "active"
                  ? "bg-brand-500 text-white"
                  : "bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-600"
              }`}
            >
              Актуальные
            </button>
            <button
              onClick={() => {
                setTab("archive");
                setSelected(null);
                setReplyTo(null);
                setEditingMessage(null);
              }}
              className={`flex-1 px-3 py-1.5 rounded-lg text-sm font-medium transition ${
                tab === "archive"
                  ? "bg-brand-500 text-white"
                  : "bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-600"
              }`}
            >
              Архив
            </button>
          </div>

          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Поиск: имя, телефон, ID, вк / тг / max…"
            className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500 mb-2"
          />

          {tab === "active" && (
            <label className="flex items-center gap-1.5 text-xs text-slate-600 dark:text-slate-400 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={onlyUnanswered}
                onChange={(e) => setOnlyUnanswered(e.target.checked)}
                className="accent-brand-500"
              />
              Только неотвеченные
            </label>
          )}
        </div>

        <div className="flex-1 overflow-y-auto">
          {listQuery.isLoading && (
            <div className="p-6 text-center text-sm text-slate-500">
              Загрузка…
            </div>
          )}
          {!listQuery.isLoading && visibleItems.length === 0 && (
            <div className="p-6 text-center text-sm text-slate-500 dark:text-slate-400">
              {tab === "active"
                ? onlyUnanswered
                  ? "Нет неотвеченных диалогов"
                  : "Нет активных диалогов"
                : "Архив пуст"}
            </div>
          )}
          {visibleItems.map((it) => {
            const active =
              selected?.platform === it.platform &&
              selected?.userId === it.user_id;
            return (
              <button
                key={`${it.platform}:${it.user_id}`}
                onClick={() => {
                  setSelected({ platform: it.platform, userId: it.user_id });
                  setReplyTo(null);
                  setEditingMessage(null);
                }}
                className={`w-full text-left px-4 py-3 border-b border-slate-100 dark:border-slate-700 transition ${
                  active
                    ? "bg-brand-50 dark:bg-brand-900/20"
                    : "hover:bg-slate-50 dark:hover:bg-slate-700/50"
                }`}
              >
                <div className="flex items-start gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <PlatformBadge platform={it.platform} />
                      <span className="font-medium text-sm text-slate-900 dark:text-slate-100 truncate">
                        {it.sender_name || it.name_age || `ID ${it.user_id}`}
                      </span>
                      {it.is_unanswered && (
                        <span className="ml-auto flex-shrink-0 px-1.5 py-0.5 bg-red-500 text-white text-[10px] font-bold rounded-full">
                          NEW
                        </span>
                      )}
                    </div>
                    {it.sender_name && (
                      <div className="text-[11px] text-slate-500 dark:text-slate-400 truncate">
                        {it.sender_username ? `@${it.sender_username}` : null}
                        {it.name_age ? `${it.sender_username ? " · " : ""}${it.name_age}` : null}
                        {it.phone ? ` · ${it.phone}` : null}
                      </div>
                    )}
                    <div className="text-xs text-slate-500 dark:text-slate-400 truncate mt-0.5">
                      {it.last_message_direction === "out" && (
                        <span className="opacity-60">Вы: </span>
                      )}
                      {it.last_message_text}
                    </div>
                    <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-0.5">
                      {timeAgo(it.last_message_at)}
                    </div>
                  </div>
                </div>
              </button>
            );
          })}
          <div ref={loadMoreRef} className="h-1" />
          {listQuery.isFetchingNextPage && (
            <div className="p-3 text-center text-xs text-slate-500">
              Загрузка…
            </div>
          )}
        </div>
      </aside>

      {/* Чат */}
      <main
        className={`${
          selected ? "flex" : "hidden md:flex"
        } flex-col flex-1 bg-slate-50 dark:bg-slate-900 min-w-0`}
      >
        {!selected ? (
          <div className="flex-1 flex items-center justify-center text-sm text-slate-500 dark:text-slate-400">
            Выберите диалог
          </div>
        ) : (
          <>
            <header className="p-3 md:p-4 bg-white dark:bg-slate-800 border-b border-slate-200 dark:border-slate-700 flex items-center gap-2">
              <button
                onClick={() => {
                  setSelected(null);
                  setReplyTo(null);
                  setEditingMessage(null);
                }}
                className="md:hidden w-8 h-8 rounded-full hover:bg-slate-100 dark:hover:bg-slate-700 flex items-center justify-center text-slate-900 dark:text-slate-100"
              >
                ←
              </button>
              <PlatformBadge platform={selected.platform} />
              <div className="flex-1 min-w-0">
                <div className="font-medium text-sm text-slate-900 dark:text-slate-100 truncate">
                  {selectedItem?.sender_name
                    || selectedItem?.name_age
                    || `ID ${selected.userId}`}
                </div>
                {selectedItem?.sender_username && (
                  <div className="text-xs text-slate-500 dark:text-slate-400 truncate">
                    @{selectedItem.sender_username}
                  </div>
                )}
                {!selectedItem?.sender_username && selectedItem?.phone && (
                  <div className="text-xs text-slate-500 dark:text-slate-400 truncate">
                    {selectedItem.phone}
                  </div>
                )}
              </div>
              {selectedItem?.application_id && (
                <Link
                  to={`/applications/${selectedItem.application_id}`}
                  className="hidden sm:inline-block text-xs px-2.5 py-1 rounded-lg bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-200 hover:bg-slate-200 dark:hover:bg-slate-600 transition"
                >
                  Заявка #{selectedItem.application_id}
                </Link>
              )}
              {tab === "active" ? (
                <button
                  onClick={() => selected && archiveMut.mutate(selected)}
                  disabled={
                    archiveMut.isPending ||
                    (selectedItem?.is_unanswered ?? false)
                  }
                  title={
                    selectedItem?.is_unanswered
                      ? "Сначала ответьте пользователю"
                      : "Переместить в архив"
                  }
                  className="text-xs px-2.5 py-1 rounded-lg bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-200 hover:bg-slate-200 dark:hover:bg-slate-600 disabled:opacity-40 disabled:cursor-not-allowed transition"
                >
                  В архив
                </button>
              ) : (
                <button
                  onClick={() => selected && unarchiveMut.mutate(selected)}
                  disabled={unarchiveMut.isPending}
                  className="text-xs px-2.5 py-1 rounded-lg bg-brand-500 text-white hover:bg-brand-600 disabled:opacity-50 transition"
                >
                  Вернуть
                </button>
              )}
            </header>

            <div
              ref={messagesContainerRef}
              onScroll={handleMessagesScroll}
              className="flex-1 overflow-y-auto p-3 md:p-4 space-y-3"
            >
              {detailQuery.isLoading && (
                <div className="text-center text-sm text-slate-500">
                  Загрузка…
                </div>
              )}
              {detail?.messages.map((m) => {
                const isEditing = editingMessage?.id === m.id;
                return (
                  <div
                    key={m.id}
                    id={`msg-${m.id}`}
                    data-message-id={m.id}
                    className={`flex group ${
                      m.direction === "out" ? "justify-end" : ""
                    } rounded-lg transition-all`}
                  >
                    {m.direction === "in" && !isEditing && (
                      <button
                        type="button"
                        onClick={() => {
                          setReplyTo(m);
                          setEditingMessage(null);
                        }}
                        title="Ответить"
                        className="self-center mr-1 w-6 h-6 rounded-full bg-slate-200 dark:bg-slate-700 text-slate-500 dark:text-slate-400 text-xs opacity-100 md:opacity-0 md:group-hover:opacity-100 transition flex-shrink-0"
                      >
                        ↩
                      </button>
                    )}

                    <div
                      className={`max-w-[78%] rounded-2xl px-3 py-2 text-sm ${
                        m.direction === "out"
                          ? "bg-brand-500 text-white"
                          : "bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100 border border-slate-200 dark:border-slate-700"
                      } ${
                        replyTo?.id === m.id
                          ? "ring-2 ring-brand-400 dark:ring-brand-600"
                          : ""
                      }`}
                    >
                      {/* Цитата */}
                      {m.reply_to && (
                        <button
                          type="button"
                          onClick={() => scrollToMessage(m.reply_to!.id)}
                          className={`block w-full text-left mb-1 pl-2 border-l-2 opacity-90 ${
                            m.direction === "out"
                              ? "border-white/60"
                              : "border-brand-400"
                          }`}
                        >
                          <div
                            className={`text-[10px] font-medium ${
                              m.direction === "out"
                                ? "text-brand-100"
                                : "text-brand-600 dark:text-brand-400"
                            }`}
                          >
                            {m.reply_to.direction === "out"
                              ? "Вы"
                              : "Пользователь"}
                          </div>
                          <div className="text-xs truncate">
                            {m.reply_to.text}
                          </div>
                        </button>
                      )}

                      <div className="text-[10px] mb-0.5 opacity-70">
                        {m.direction === "out" ? "Вы" : "Пользователь"} ·{" "}
                        {new Date(m.created_at).toLocaleString("ru-RU", {
                          day: "2-digit",
                          month: "2-digit",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                        {m.edited_at && (
                          <span
                            title={`Изменено: ${new Date(
                              m.edited_at,
                            ).toLocaleString("ru-RU")}`}
                            className="ml-1 opacity-80"
                          >
                            · изменено
                          </span>
                        )}
                      </div>

                      {isEditing ? (
                        <div className="mt-1" onClick={(e) => e.stopPropagation()}>
                          <textarea
                            value={editText}
                            onChange={(e) => setEditText(e.target.value)}
                            autoFocus
                            rows={Math.min(6, editText.split("\n").length + 1)}
                            className={`w-full text-sm rounded-md px-2 py-1 resize-y focus:outline-none focus:ring-2 ${
                              m.direction === "out"
                                ? "bg-white/20 text-white placeholder:text-white/60 focus:ring-white/60"
                                : "bg-slate-100 dark:bg-slate-900 text-slate-900 dark:text-slate-100 focus:ring-brand-500"
                            }`}
                            onKeyDown={(e) => {
                              if (e.key === "Enter" && !e.shiftKey) {
                                e.preventDefault();
                                saveEdit();
                              } else if (e.key === "Escape") {
                                e.preventDefault();
                                cancelEdit();
                              }
                            }}
                          />
                          <div className="flex gap-1.5 mt-1.5 justify-end">
                            <button
                              type="button"
                              onClick={cancelEdit}
                              className={`text-[11px] px-2 py-0.5 rounded ${
                                m.direction === "out"
                                  ? "bg-white/20 hover:bg-white/30 text-white"
                                  : "bg-slate-200 hover:bg-slate-300 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-700 dark:text-slate-200"
                              }`}
                            >
                              Отмена
                            </button>
                            <button
                              type="button"
                              onClick={saveEdit}
                              disabled={editMut.isPending}
                              className={`text-[11px] px-2 py-0.5 rounded font-medium disabled:opacity-50 ${
                                m.direction === "out"
                                  ? "bg-white text-brand-600 hover:bg-white/90"
                                  : "bg-brand-500 hover:bg-brand-600 text-white"
                              }`}
                            >
                              {editMut.isPending ? "Сохраняю…" : "Сохранить"}
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="whitespace-pre-wrap break-words">
                          {m.text}
                        </div>
                      )}
                    </div>

                    {m.direction === "out" && !isEditing && (
                      <div className="self-center ml-1 flex flex-col gap-0.5">
                        <button
                          type="button"
                          onClick={() => startEdit(m)}
                          title="Редактировать"
                          className="w-6 h-6 rounded-full bg-slate-200 dark:bg-slate-700 text-slate-500 dark:text-slate-400 text-xs opacity-100 md:opacity-0 md:group-hover:opacity-100 transition"
                        >
                          ✏️
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setReplyTo(m);
                            setEditingMessage(null);
                          }}
                          title="Ответить"
                          className="w-6 h-6 rounded-full bg-slate-200 dark:bg-slate-700 text-slate-500 dark:text-slate-400 text-xs opacity-100 md:opacity-0 md:group-hover:opacity-100 transition"
                        >
                          ↩
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
              <div ref={messagesEndRef} />
            </div>

            {/* Превью цитаты над полем ввода */}
            {replyTo && !editingMessage && (
              <div className="px-3 pt-2 pb-1 bg-slate-100 dark:bg-slate-700 border-t border-slate-200 dark:border-slate-700 flex items-start gap-2">
                <div className="flex-1 min-w-0 border-l-2 border-brand-500 pl-2">
                  <div className="text-[10px] font-medium text-brand-600 dark:text-brand-400">
                    Ответ на сообщение{" "}
                    {replyTo.direction === "out" ? "(вы)" : "пользователя"}
                  </div>
                  <div className="text-xs text-slate-600 dark:text-slate-300 truncate">
                    {replyTo.text}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setReplyTo(null)}
                  className="w-6 h-6 rounded-full text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 flex-shrink-0"
                  title="Отменить"
                >
                  ×
                </button>
              </div>
            )}

            <form
              onSubmit={onSubmitReply}
              className="p-3 bg-white dark:bg-slate-800 border-t border-slate-200 dark:border-slate-700 flex gap-2"
            >
              <input
                type="text"
                autoComplete="off"
                value={replyText}
                onChange={(e) => setReplyText(e.target.value)}
                placeholder={
                  editingMessage
                    ? "Сначала завершите редактирование"
                    : "Написать сообщение…"
                }
                disabled={!!editingMessage}
                className="flex-1 border border-slate-300 dark:border-slate-600 rounded-full px-4 py-2.5 text-sm bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500 disabled:opacity-60"
              />
              <button
                type="submit"
                disabled={
                  !!editingMessage ||
                  replyMut.isPending ||
                  !replyText.trim()
                }
                className="w-11 h-11 rounded-full bg-brand-500 hover:bg-brand-600 text-white disabled:opacity-50 flex items-center justify-center transition"
                aria-label="Отправить"
              >
                →
              </button>
            </form>
          </>
        )}
      </main>
    </div>
  );
}