import { useTheme } from "../contexts/ThemeContext";

export function ThemeToggle() {
  const { resolved, setTheme } = useTheme();
  return (
    <button
      onClick={() => setTheme(resolved === "dark" ? "light" : "dark")}
      className="w-full text-sm py-2 rounded-lg text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700 flex items-center justify-center gap-2"
      title={resolved === "dark" ? "Светлая тема" : "Тёмная тема"}
    >
      {resolved === "dark" ? "☀️ Светлая" : "🌙 Тёмная"}
    </button>
  );
}