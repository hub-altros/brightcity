import { Toaster } from "sonner";
import { useTheme } from "../contexts/ThemeContext";

export function ThemedToaster() {
  const { resolved } = useTheme();
  return (
    <Toaster
      position="top-right"
      richColors
      theme={resolved}   // 'light' | 'dark'
      closeButton
      expand
      duration={3500}
    />
  );
}