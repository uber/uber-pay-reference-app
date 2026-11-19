import pg from 'pg';
import { readFileSync } from 'node:fs';
import type { Config } from './config.js';

pg.types.setTypeParser(20, (value) => BigInt(value));
export type Connection = pg.PoolClient;

export class Store {
  readonly pool: pg.Pool;
  constructor(readonly config: Config) {
    this.pool = new pg.Pool({
      connectionString: config.databaseUrl,
      application_name: config.clientId,
      max: 10,
      connectionTimeoutMillis: 5000,
      statement_timeout: 5000,
      lock_timeout: 1500,
    });
    this.pool.on('connect', (client) =>
      client.on('error', () => console.error('database active connection failed; transaction will retry')),
    );
    this.pool.on('error', () => console.error('database idle connection failed; pool will reconnect'));
  }
  async init(): Promise<void> {
    await this.pool.query(readFileSync(this.config.migrationFile, 'utf8'));
    await this.transaction(async (db) => {
      await db.query(
        `INSERT INTO accounts(client_id,account_id,currency,available_e5,balance_e5)
         VALUES($1,'demo-settlement','BRL',1000000000000000000,1000000000000000000),
               ($1,'demo-account','BRL',1000000000000000000,1000000000000000000),
               ($1,'insufficient-account','BRL',100000,100000)
         ON CONFLICT DO NOTHING`,
        [this.config.clientId],
      );
    });
  }
  async transaction<T>(fn: (db: Connection) => Promise<T>, serialize = true): Promise<T> {
    const db = await this.pool.connect();
    let discard: Error | undefined;
    try {
      await db.query('BEGIN');
      if (serialize) await db.query('SELECT pg_advisory_xact_lock(hashtext($1))', [this.config.clientId]);
      const value = await fn(db);
      await db.query('COMMIT');
      return value;
    } catch (error) {
      try {
        await db.query('ROLLBACK');
      } catch {
        discard = new Error('Unusable database connection');
      }
      if (/^(08|57P0)/.test(String((error as { code?: string }).code ?? '')))
        discard = new Error('Database connection lost');
      throw error;
    } finally {
      db.release(discard);
    }
  }
  async close(): Promise<void> {
    await this.pool.end();
  }
}
