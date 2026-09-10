import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (request, incoming) => {
            if (incoming.headers.origin === 'http://127.0.0.1:5173') {
              request.setHeader('origin', 'http://127.0.0.1:8765');
            }
          });
        },
      },
    },
  },
});
