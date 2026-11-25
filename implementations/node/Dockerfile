FROM node:24.8.0-bookworm-slim@sha256:cadbfafeb6baf87eaaffa40b3640209c4b7fd38cebde65059d15bc39cd636b85
WORKDIR /workspace
COPY implementations/node/package*.json implementations/node/
RUN cd implementations/node && npm ci --ignore-scripts --registry=https://registry.npmjs.org/
COPY contracts/ contracts/
COPY storage/ storage/
COPY implementations/node/ implementations/node/
RUN cd implementations/node && npm run build
ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080
USER node
CMD ["node", "implementations/node/dist/main.js"]
