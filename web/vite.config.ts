import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.OBZERVE_API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": apiTarget,
      "/readyz": apiTarget,
    },
  },
});
