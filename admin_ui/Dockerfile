# Multi-stage build for React admin UI
FROM node:22-alpine AS builder

# Set working directory
WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy source code
COPY . .

# Bake the API base URL into the Vite bundle. Default is the relative path
# `/api/v1`, which keeps the admin SPA same-origin: served from
# admin.aqua-element.uz, requests go to admin.aqua-element.uz/api/v1 and
# hit the relaxed `admin_api_limit` nginx zone (40 r/s, burst 120) instead
# of the strict public `api_limit` (10 r/s, burst 20). The value flows in
# via docker-compose's `build.args` so each environment can override it.
ARG VITE_API_URL=/api/v1
ENV VITE_API_URL=${VITE_API_URL}

# Build the application
RUN npm run build

# Production stage
FROM nginx:alpine

# Install curl for health checks
RUN apk --no-cache add curl

# Copy built application
COPY --from=builder /app/build /usr/share/nginx/html

# Copy custom nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Expose port
EXPOSE 80

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:80 || exit 1

# Start nginx
CMD ["nginx", "-g", "daemon off;"]
