import { defineConfig } from "vite";

// `npm run dev` serves the page on :3000; Icechunk reads go to `uv run backup/server.py` (:8001).
export default defineConfig({
  server: { port: 3000, proxy: { "/icechunk": "http://127.0.0.1:8001" } },
  build: { target: "es2022" },
});
