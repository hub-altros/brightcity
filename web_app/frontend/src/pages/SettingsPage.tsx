import { useTheme, type Theme } from '../contexts/ThemeContext';

export function SettingsPage() {
  const { theme, resolved, setTheme } = useTheme();

  const options: { value: Theme; label: string; hint: string; icon: string }[] = [
    { value: 'light',  label: 'Светлая',  hint: 'Всегда светлый интерфейс',  icon: '☀️' },
    { value: 'dark',   label: 'Тёмная',   hint: 'Всегда тёмный интерфейс',   icon: '🌙' },
    { value: 'system', label: 'Как в системе', hint: 'Следовать теме ОС',     icon: '💻' },
  ];

  return (
    <div className="max-w-2xl mx-auto p-4 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight mb-6">Настройки</h1>

      <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-4">
        <div className="mb-3">
          <div className="font-medium">Тема оформления</div>
          <div className="text-xs text-slate-500">
            Сейчас: {resolved === 'dark' ? 'тёмная' : 'светлая'}
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          {options.map(opt => (
            <button
              key={opt.value}
              onClick={() => setTheme(opt.value)}
              className={`text-left p-3 rounded-lg border transition ${
                theme === opt.value
                  ? 'border-brand-500 bg-brand-50 dark:bg-brand-900/20'
                  : 'border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-700'
              }`}
            >
              <div className="text-2xl mb-1">{opt.icon}</div>
              <div className="font-medium text-sm">{opt.label}</div>
              <div className="text-xs text-slate-500 mt-0.5">{opt.hint}</div>
            </button>
          ))}
        </div>
      </div>

      <p className="mt-4 text-xs text-slate-500">
        Настройки сохраняются в этом браузере. На других устройствах будет своя тема.
      </p>
    </div>
  );
}