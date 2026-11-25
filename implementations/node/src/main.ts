import { loadConfig } from './config.js';
import { Store } from './store.js';
import { createApp } from './app.js';
import { Worker } from './worker.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const store = new Store(config);
  await store.init();
  const { app, service } = createApp(config, store);
  const worker = new Worker(service);
  const server = app.listen(config.port, config.host, () => {
    console.log(`Local synthetic-money Node gateway listening on ${config.host}:${config.port}`);
    worker.start();
    service.executor?.start();
  });
  async function stop(): Promise<void> {
    server.close();
    await worker.stop();
    await service.executor?.stop();
    await store.close();
    process.exit(0);
  }
  process.once('SIGINT', () => {
    void stop();
  });
  process.once('SIGTERM', () => {
    void stop();
  });
}
main().catch(() => {
  console.error('Gateway startup failed; check configuration, contract, keys and database');
  process.exit(1);
});
