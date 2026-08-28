# --- web build stage ---
FROM node:20-alpine AS deps
WORKDIR /app
COPY apps/web/package.json ./
# The web app depends on the local dnd-sdk package via `file:../../libs/...`,
# so place it at /libs to match the relative reference.
COPY libs/typescript/dnd-sdk /libs/typescript/dnd-sdk
RUN cd /libs/typescript/dnd-sdk && npm install --no-audit --no-fund && npm run build
RUN npm install --no-audit --no-fund

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
# Absolute destination: npm's file: install links the SDK into node_modules
# as ../../../libs/typescript/dnd-sdk, which resolves to /libs (not /app/libs).
COPY --from=deps /libs /libs
COPY apps/web .
ENV NEXT_TELEMETRY_DISABLED=1
# npm's file: install links @dnd-wiki/sdk into node_modules, but that link can
# be clobbered by the COPY above (esp. Windows junctions serialized as broken
# Linux symlinks). Re-link explicitly against the SDK built in the deps stage.
RUN rm -rf node_modules/@dnd-wiki/sdk && ln -s /libs/typescript/dnd-sdk node_modules/@dnd-wiki/sdk && npm run build

# --- runtime ---
FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN addgroup -S nodejs && adduser -S nextjs -G nodejs

COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
