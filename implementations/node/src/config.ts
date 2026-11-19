import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createPublicKey } from 'node:crypto';

export function localUrl(value: string): URL {
  const url = new URL(value);
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !['localhost', '127.0.0.1', '[::1]', '::1', 'mock-uber', 'processor'].includes(url.hostname)
  ) {
    throw new Error('Local mode refuses nonlocal outbound URLs or URL credentials');
  }
  return url;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env) {
  const get = (key: string, fallback: string): string => env[key] ?? fallback;
  const int = (key: string, fallback: string, min = 1): number => {
    const n = Number(get(key, fallback));
    if (!Number.isSafeInteger(n) || n < min) throw new Error(`Invalid ${key}`);
    return n;
  };
  if (get('MODE', 'local') !== 'local') throw new Error('Only local synthetic-money mode is implemented');
  const root = resolve(get('REPO_ROOT', '.'));
  const basePath = get('BASE_PATH', '/uberpay/v1');
  if (!/^\/[A-Za-z0-9/_-]*[A-Za-z0-9_-]$/.test(basePath)) throw new Error('Invalid BASE_PATH');
  const callbackBase = localUrl(get('UBER_CALLBACK_BASE_URL', 'http://127.0.0.1:9090'))
    .toString()
    .replace(/\/$/, '');
  const tokenUrl = localUrl(get('UBER_TOKEN_URL', 'http://127.0.0.1:9090/oauth/token')).toString();
  const publicBase = localUrl(get('PUBLIC_BASE_URL', `http://127.0.0.1:${get('PORT', '8080')}${basePath}`))
    .toString()
    .replace(/\/$/, '');
  const publicKey = readFileSync(
    resolve(root, get('UBER_PUBLIC_KEY_FILE', '.local/uber-public.pem')),
    'utf8',
  );
  const key = createPublicKey(publicKey);
  if (key.asymmetricKeyType !== 'rsa' || (key.asymmetricKeyDetails?.modulusLength ?? 0) < 2048) {
    throw new Error('A valid RSA public key of at least 2048 bits is required');
  }
  const databaseUrl = get('DATABASE_URL', 'postgresql://uberpay:uberpay@127.0.0.1:5432/uberpay');
  const database = new URL(databaseUrl);
  if (
    !['postgres:', 'postgresql:'].includes(database.protocol) ||
    !['localhost', '127.0.0.1', '[::1]', '::1', 'postgres', 'db'].includes(database.hostname)
  ) {
    throw new Error('Local mode requires a local PostgreSQL instance');
  }
  const processorMode = get('PROCESSOR_MODE', 'local');
  if (!['local', 'http'].includes(processorMode)) throw new Error('Invalid PROCESSOR_MODE');
  const processorUrl = localUrl(get('PROCESSOR_URL', 'http://127.0.0.1:9080')).toString().replace(/\/$/, '');
  const processorToken = get('PROCESSOR_AUTH_TOKEN', '');
  const processorKey = get('PROCESSOR_STATE_KEY', '');
  if (
    processorMode === 'http' &&
    (processorToken.length < 32 ||
      Buffer.from(processorKey, 'base64').length !== 32 ||
      Buffer.from(processorKey, 'base64').toString('base64') !== processorKey)
  )
    throw new Error('HTTP processor requires explicit credentials and a persistent 256-bit state key');
  const clientId = get('CLIENT_ID', 'demo-client');
  const enableControl = get('ENABLE_TEST_CONTROL', 'false') === 'true';
  const controlSecret = get('TEST_CONTROL_SECRET', '');
  if (enableControl && controlSecret.length < 16)
    throw new Error('Test control requires a random secret of at least 16 characters');
  return {
    root,
    basePath,
    callbackBase,
    tokenUrl,
    publicBase,
    publicKey,
    clientId,
    processorMode,
    processorUrl,
    processorToken,
    processorKey,
    processorTimeoutMs: int('PROCESSOR_TIMEOUT_MS', '2000'),
    processorLeaseMs: int('PROCESSOR_LEASE_MS', '15000'),
    processorWaitMs: int('PROCESSOR_WAIT_MS', '250'),
    processorPollMs: int('PROCESSOR_POLL_MS', '200'),
    processorMaxFailures: int('PROCESSOR_MAX_FAILURES', '8'),
    port: int('PORT', '8080'),
    host: get('HOST', '127.0.0.1'),
    databaseUrl,
    contractFile: get('CONTRACT_FILE', 'contracts/runtime.json'),
    migrationFile: resolve(root, get('MIGRATION_FILE', 'storage/migrations/001_init.sql')),
    keyId: get('UBER_KEY_ID', 'demo-uber-key'),
    clientSecret: get('CLIENT_SECRET', 'demo-secret'),
    uberClientId: get('UBER_CLIENT_ID', 'demo-uber-client'),
    uberClientSecret: get('UBER_CLIENT_SECRET', 'demo-uber-secret'),
    allowedRedirects: get(
      'ALLOWED_REDIRECT_URIS',
      'http://127.0.0.1:9090/return,uber://ubercash_topup',
    ).split(','),
    skewSeconds: int('SIGNATURE_MAX_SKEW_SECONDS', '300'),
    asyncDelayMs: int('ASYNC_DELAY_MS', '500', 0),
    workerIntervalMs: int('WORKER_INTERVAL_MS', '100'),
    maxAttempts: int('CALLBACK_MAX_ATTEMPTS', '5'),
    retryMs: int('CALLBACK_RETRY_MS', '200'),
    enableControl,
    controlSecret,
    processorPauseAfterPrepare:
      enableControl && get('PROCESSOR_TEST_PAUSE_AFTER_PREPARE', 'false') === 'true',
    actionTtlMs: int('ACTION_TTL_MS', '600000'),
    refreshTtlSeconds: int('REFRESH_TOKEN_TTL_SECONDS', '2592000'),
    strictAccountName: get('STRICT_ACCOUNT_NAME', 'false') === 'true',
    oauthRequired: get('OAUTH_REQUIRED', 'false') === 'true',
  };
}
export type Config = ReturnType<typeof loadConfig>;
