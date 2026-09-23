import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { rename } from 'node:fs/promises';

// GitHub Pages serves <base>/index.html. The demo entry is demo.html so the
// release build keeps owning index.html; rename it during the demo build only.
const asIndex: Plugin = {
  name: 'crackrag-demo-index',
  apply: 'build',
  async closeBundle() {
    await rename('dist-demo/demo.html', 'dist-demo/index.html');
  },
};

// Static replay build for GitHub Pages: https://xjfyrh.github.io/CrackRAG/demo/
// Separate config on purpose — the release build in vite.config.ts must keep
// serving from '/', and this one must never touch it.
export default defineConfig({
  base: '/CrackRAG/demo/',
  // Compile-time flag the app reads through import.meta.env. The release build
  // never defines it, so `readOnly` and the restore guard stay false there.
  define: { 'import.meta.env.VITE_DEMO_REPLAY': JSON.stringify('1') },
  plugins: [react(), asIndex],
  build: {
    outDir: 'dist-demo',
    emptyOutDir: true,
    rollupOptions: { input: 'demo.html' },
  },
});
