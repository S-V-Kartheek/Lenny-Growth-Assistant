# Two stages: build the static bundle with Node, then serve it with a plain
# static file server. Vite's own dev server is not used here -- it is meant
# for local iteration with HMR, not for serving a container's long-running
# process.
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# VITE_API_BASE_URL is baked into the bundle at build time (Vite inlines
# import.meta.env.* at build, not at container start) -- it defaults to
# localhost:8000 because the browser, not this container, makes the API
# call, and the API's port is published to the host either way.
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM node:22-slim AS serve
WORKDIR /app
RUN npm install -g serve@14
COPY --from=build /app/dist ./dist
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 4173
CMD ["serve", "-s", "dist", "-l", "4173"]
