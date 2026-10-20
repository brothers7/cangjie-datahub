# ---- build stage ----
FROM node:20-alpine AS build
WORKDIR /app

# 严格按 package-lock.json 安装，保证可复现
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --registry=https://registry.npmjs.org

COPY frontend/ ./
RUN npm run build

# ---- run stage：非 root nginx（官方 unprivileged 镜像，监听 8080）----
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
