# ---- build stage ----
FROM node:20-alpine AS build
WORKDIR /app

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --registry=https://registry.npmjs.org || npm install

COPY frontend/ ./
RUN npm run build

# ---- run stage ----
FROM nginx:1.27-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
