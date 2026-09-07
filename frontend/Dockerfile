# The dashboard. Two stages: the build toolchain never reaches the runtime.
FROM node:22-alpine AS build
WORKDIR /app
# Dependencies first, so a source-only change does not reinstall them.
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Rewrite destinations are resolved during `next build` and written into the
# route manifest -- setting them at runtime does nothing, and the server quietly
# proxies to localhost:8000 and answers 500 on every API call. They are build
# inputs, so they are declared here, defaulting to the compose service names.
ARG API_ORIGIN=http://api:8000
ARG MEDIAMTX_ORIGIN=http://mediamtx:8889
ARG MEDIAMTX_HLS_ORIGIN=http://mediamtx:8888
ENV API_ORIGIN=$API_ORIGIN
ENV MEDIAMTX_ORIGIN=$MEDIAMTX_ORIGIN
ENV MEDIAMTX_HLS_ORIGIN=$MEDIAMTX_HLS_ORIGIN
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
# Next's standalone trace: the server plus only the modules it imports.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
# Next binds to $HOSTNAME, which Docker sets to the container id. Left alone it
# listens on that name only and a published port reaches nothing.
ENV HOSTNAME=0.0.0.0
EXPOSE 3000
USER node
CMD ["node", "server.js"]
