import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { authApi } from "../api/auth";

export default function Login() {
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const navigate = useNavigate();

  const m = useMutation({
    mutationFn: () => authApi.login(login, password),
    onSuccess: (data) => navigate(data.redirect),
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <div className="max-w-md mx-auto mt-8 md:mt-20 px-4">
      <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 md:p-8">
        <div className="text-center mb-6">
          <div className="w-16 h-16 mx-auto mb-3 rounded-2xl bg-gradient-to-br from-brand-500 to-brand-700 flex items-center justify-center text-white text-2xl font-bold">
            ЯГ
          </div>
          <h1 className="text-xl font-semibold">Вход в админку</h1>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            m.mutate();
          }}
        >
          <label className="block text-sm font-medium mb-1.5">Логин</label>
          <input
            value={login}
            onChange={(e) => setLogin(e.target.value)}
            required
            autoFocus
            className="w-full border border-slate-300 rounded-lg px-3 py-3 mb-4"
          />
          <label className="block text-sm font-medium mb-1.5">Пароль</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            className="w-full border border-slate-300 rounded-lg px-3 py-3 mb-6"
          />
          <button
            type="submit"
            disabled={m.isPending}
            className="w-full bg-brand-500 hover:bg-brand-600 text-white font-semibold rounded-lg py-3 disabled:opacity-50"
          >
            {m.isPending ? "Вход…" : "Войти"}
          </button>
        </form>
      </div>
    </div>
  );
}