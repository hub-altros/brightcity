import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { authApi } from "../api/auth";

export default function TwoFAVerify() {
  const [code, setCode] = useState("");
  const [useRecovery, setUseRecovery] = useState(false);
  const navigate = useNavigate();
  const qc = useQueryClient();

  const m = useMutation({
    mutationFn: () => authApi.verify2fa(code, useRecovery),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin"] });
      navigate("/applications");
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <div className="max-w-md mx-auto mt-8 md:mt-20 px-4">
      <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 md:p-8">
        <h1 className="text-xl font-semibold mb-2">Двухфакторная аутентификация</h1>
        <p className="text-sm text-slate-500 mb-6">
          {useRecovery
            ? "Введите резервный код XXXX-XXXX-XXXX."
            : "Введите 6-значный код из приложения."}
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            m.mutate();
          }}
        >
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            required
            autoFocus
            inputMode="numeric"
            autoComplete="one-time-code"
            className="w-full text-center text-2xl tracking-[0.4em] font-mono border border-slate-300 rounded-lg px-3 py-4 mb-4"
          />
          <button
            type="submit"
            disabled={m.isPending}
            className="w-full bg-brand-500 hover:bg-brand-600 text-white font-semibold rounded-lg py-3 disabled:opacity-50"
          >
            Подтвердить
          </button>
        </form>
        <button
          onClick={() => setUseRecovery(!useRecovery)}
          className="mt-3 w-full text-sm text-brand-600 hover:underline"
        >
          {useRecovery ? "← Использовать код из приложения" : "Использовать резервный код"}
        </button>
      </div>
    </div>
  );
}