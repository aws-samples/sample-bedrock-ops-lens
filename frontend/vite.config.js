import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Customer build serves from /, no /bedrock prefix.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Local-dev convenience: forward /api → backend on 8001.
      '/api': { target: 'http://127.0.0.1:8001', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
});
