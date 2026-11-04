import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { authApi } from "../api/auth";

export default function TwoFASetup() {
  const [code, setCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const navigate = useNavigate();
  const qc = useQueryClient();

  const setup = useQuery({
    queryKey: ["2fa-setup"],
    queryFn: authApi.setup2fa,
    staleTime: Infinity,
  });

  const confirm = useMutation({
    mutationFn: () => authApi.confirm2fa(code),
    onSuccess: (data) => setRecoveryCodes(data.recovery_codes),
    onError: (e: Error) => toast.error(e.message),
  });

  const complete = useMutation({
    mutationFn: authApi.completeSetup,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin"] });
      navigate("/applications");
    },
  });

  if (recoveryCodes) {
    return (
      <div className="max-w-md mx-auto mt-8 md:mt-20 px-4">
        <div className="bg-white rounded-2xl border-2 border-amber-300 p-6 md:p-8">
          <h1 className="text-xl font-semibold mb-2">Сохраните резервные коды</h1>
          <p className="text-sm text-slate-500 mb-4">
            Каждый код одноразовый. <b>Больше мы их не покажем.</b>
          </p>
          <div className="bg-slate-50 rounded-lg p-4 font-mono text-sm grid grid-cols-2 gap-2 mb-4">
            {recoveryCodes.map((c) => (
              <div key={c}>{c}</div>
            ))}
          </div>
          <button
            onClick={() => complete.mutate()}
            disabled={complete.isPending}
            className="w-full bg-brand-500 hover:bg-brand-600 text-white font-semibold rounded-lg py-3 disabled:opacity-50"
          >
            Я сохранил(а) — войти
          </button>
        </div>
      </div>
    );
  }

  if (setup.isLoading || !setup.data) {
    return <div className="text-center mt-20 text-slate-500">Загрузка…</div>;
  }

  return (
    <div className="max-w-md mx-auto mt-8 md:mt-20 px-4">
      <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 md:p-8">
        <h1 className="text-xl font-semibold mb-2">Настройте 2FA</h1>
        <p className="text-sm text-slate-500 mb-4">
          Откройте Google Authenticator / Authy / 1Password и отсканируйте QR.
        </p>
        <img
          src={setup.data.qr_data_url}
          alt="QR"
          className="mx-auto w-48 h-48 mb-4 rounded-lg border border-slate-200"
        />
        <p className="text-xs text-slate-500 mb-4 text-center">
          Или введите вручную:{" "}
          <code className="font-mono bg-slate-100 px-1.5 py-0.5 rounded">
            {setup.data.secret}
          </code>
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            confirm.mutate();
          }}
        >
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            required
            inputMode="numeric"
            maxLength={6}
            autoFocus
            className="w-full text-center text-2xl tracking-[0.5em] font-mono border border-slate-300 rounded-lg px-3 py-4 mb-4"
          />
          <button
            type="submit"
            disabled={confirm.isPending}
            className="w-full bg-brand-500 hover:bg-brand-600 text-white font-semibold rounded-lg py-3 disabled:opacity-50"
          >
            Активировать 2FA
          </button>
        </form>
      </div>
    </div>
  );
}