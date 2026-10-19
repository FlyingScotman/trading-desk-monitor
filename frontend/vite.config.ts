/// <reference types="vitest" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: '/app/',
  plugins: [react()],
  server: { proxy: { '/api': process.env.DESK_API ?? 'http://127.0.0.1:8000' } },
  test: { environment: 'node' },
});
