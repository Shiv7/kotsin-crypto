import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: the backend runs on :8400; everything under /api and /ws is proxied there.
// Prod: the backend serves frontend/dist itself, so there is no proxy and no second port.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8400',
      '/ws': { target: 'ws://127.0.0.1:8400', ws: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
