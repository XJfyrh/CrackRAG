import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// '/' is the release deployment: Go serves the built assets from the same
// origin as the API, so no subpath is involved.
export default defineConfig({
  base: '/',
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:18086', '/healthz': 'http://127.0.0.1:18086' } },
});
