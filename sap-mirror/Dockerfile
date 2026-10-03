# SAP Mirror for the ECS Fargate fallback (SRD 6.15). Same entry point as on SAP BTP, with
# the `aws` profile: in-memory SQLite and OIDC access tokens instead of HANA and XSUAA.
FROM node:24.21.0-bookworm-slim

ENV CDS_ENV=aws PORT=4004
WORKDIR /app
RUN npm install --global pnpm@12.5.1 && npm cache clean --force
COPY package.json pnpm-lock.yaml .npmrc ./
RUN pnpm install --frozen-lockfile --prod
COPY db ./db
COPY srv ./srv
USER node
EXPOSE 4004
CMD ["node", "node_modules/@sap/cds/bin/serve.js"]
