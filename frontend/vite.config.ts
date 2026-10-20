/// <reference types="vitest" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: '/app/',
  plugins: [react()],
  // /desk-nav.js: the navigation script of the prototypes, served by desk serve next to /app/
  server: { proxy: Object.fromEntries(['/api', '/desk-nav.js'].map(p => [p, process.env.DESK_API ?? 'http://127.0.0.1:8100'])) },
  test: { environment: 'node' },
});
