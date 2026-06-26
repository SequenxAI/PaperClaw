// Temporary dev config for a SECOND instance — proxies /api to an alternate backend
// port (BACKEND_PORT env, default 8231) so it doesn't collide with the main :8230.
// Not tracked / not for production; delete when done testing.
import { resolve } from 'path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backendPort = process.env.BACKEND_PORT || '8231'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': resolve(__dirname, 'src') } },
  server: {
    proxy: {
      '/api': { target: `http://127.0.0.1:${backendPort}`, changeOrigin: true }
    }
  },
  build: { outDir: 'dist/web' }
})
