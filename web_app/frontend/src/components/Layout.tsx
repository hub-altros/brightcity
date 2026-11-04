import { Link, Outlet, useLocation } from "react-router-dom";
import { useAdmin, useLogout } from "../hooks/useAdmin";
import { ThemeToggle } from "./ThemeToggle";

const NAV = [
  { to: "/applications", label: "Заявки" },
  { to: "/chats",        label: "Переписка" },
  { to: "/gdpr",         label: "GDPR" },
  { to: "/stats",        label: "Статистика" },
  { to: "/bans",         label: "Баны" },
  { to: "/settings",     label: "Настройки" },
];

export default function Layout() {
  const { data: admin } = useAdmin();
  const location = useLocation();
  const doLogout = useLogout();

  if (!admin) return null;

  // Находим активный пункт по URL — «to» уникален, используем его как id.
  const active = NAV.find((n) => location.pathname.startsWith(n.to))?.to;

  return (
    <div className="min-h-screen">
      <aside className="hidden md:flex fixed inset-y-0 left-0 w-64 bg-white border-r border-slate-200 flex-col z-30">
        <div className="px-5 py-5 border-b border-slate-100 flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-brand-100 text-brand-700 flex items-center justify-center font-semibold">
            {(admin.display_name || admin.login).slice(0, 1).toUpperCase()}
          </div>
          <div>
            <div className="font-semibold text-sm">{admin.display_name}</div>
            <div className="text-xs text-slate-500">Администратор</div>
          </div>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-1">
          {NAV.map((n) => (
            <Link
              key={n.to}
              to={n.to}
              className={`block px-3 py-2.5 rounded-lg text-sm ${
                active === n.to
                  ? "bg-brand-50 text-brand-700 font-semibold"
                  : "text-slate-700 hover:bg-slate-100"
              }`}
            >
              {n.label}
            </Link>
          ))}
        </nav>
        <div className="p-3 border-t border-slate-100 dark:border-slate-800">
          <ThemeToggle />
          <button
            onClick={doLogout}
            className="w-full text-sm text-center py-2 rounded-lg text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700"
          >
            Выйти
          </button>
        </div>
      </aside>

      <div className="md:pl-64 min-h-screen pb-24 md:pb-0">
        <Outlet />
      </div>

      {active && (
        <nav className="md:hidden fixed bottom-0 inset-x-0 bg-white/95 backdrop-blur border-t border-slate-200 z-40 safe-bottom">
          <div className="grid grid-cols-6 h-14">
            {NAV.map((n) => (
              <Link
                key={n.to}
                to={n.to}
                className={`flex items-center justify-center text-[10px] font-medium leading-tight px-1 text-center ${
                  active === n.to ? "text-brand-600" : "text-slate-400"
                }`}
              >
                {n.label}
              </Link>
            ))}
          </div>
        </nav>
      )}
    </div>
  );
}