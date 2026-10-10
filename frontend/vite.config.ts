import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/api": {
        target: process.env.TAMAM_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
      "/artifacts": {
        target: process.env.TAMAM_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
})
