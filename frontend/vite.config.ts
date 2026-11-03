import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  // Never load dotenv files; Phase 0 needs no credentials.
  envDir: false,
  server: {
    proxy: {
      '/analyses': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
});
