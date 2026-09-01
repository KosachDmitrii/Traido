# Build the Vite desk, then serve the static bundle from nginx.
FROM node:22-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY . .

# Baked in at build time — Vite inlines VITE_* at compile, not at runtime.
# Railway: set VITE_API_BASE_URL=https://${{backend.RAILWAY_PUBLIC_DOMAIN}}
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build


FROM nginx:1.27-alpine AS runtime

# Railway injects PORT; local Docker defaults to 80.
ENV PORT=80
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/templates/default.conf.template

# Official nginx image runs envsubst on /etc/nginx/templates/*.template
# into /etc/nginx/conf.d/ before starting — so ${PORT} is substituted.

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -q --spider "http://127.0.0.1:${PORT}/" || exit 1

CMD ["nginx", "-g", "daemon off;"]
