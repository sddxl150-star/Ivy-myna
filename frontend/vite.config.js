import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: '../src/web/public',
    emptyOutDir: true,
    cssMinify: false,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash].[ext]'
      }
    }
  },
  server: {
    proxy: {
      '/admin': 'http://localhost:3455',
      '/auth': 'http://localhost:3455',
      '/bot': 'http://localhost:3455',
      '/uploads': 'http://localhost:3455',
      '/health': 'http://localhost:3455',
      '/ws': { target: 'ws://localhost:3455', ws: true }
    }
  }
})
