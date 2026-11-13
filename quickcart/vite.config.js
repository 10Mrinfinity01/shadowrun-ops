import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The shop's API (storefront) runs on port 8080. The dev server forwards /api there, so the app works
// the same from this laptop, from Laptop B (http://<Laptop A IP>:3000) and from a phone on the hotspot.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 3000,
    strictPort: true,
    proxy: { '/api': { target: 'http://localhost:8080', changeOrigin: true } },
  },
})
