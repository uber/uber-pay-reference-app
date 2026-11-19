import pg from 'pg';
import { readFileSync } from 'node:fs';
import type { Config } from './config.js';

pg.types.setTypeParser(20, value => BigInt(value));
export type Connection = pg.PoolClient;

export class Store {
  readonly pool: pg.Pool;
  constructor(readonly config: Config) {
    this.pool = new pg.Pool({ connectionString: config.databaseUrl, max: 10, connectionTimeoutMillis: 5000 });
  }
  async init(): Promise<void> {
    await this.pool.query(readFileSync(this.config.migrationFile, 'utf8'));
    await this.transaction(async db => {
      await db.query(
        `INSERT INTO accounts(client_id,account_id,currency,available_e5,balance_e5)
         VALUES($1,'demo-account','BRL',1000000000000000000,1000000000000000000),
               ($1,'insufficient-account','BRL',100000,100000)
         ON CONFLICT DO NOTHING`, [this.config.clientId],
      );
    });
  }
  async transaction<T>(fn: (db: Connection) => Promise<T>): Promise<T> {
    const db = await this.pool.connect();
    try {
      await db.query('BEGIN');
      await db.query('SELECT pg_advisory_xact_lock(hashtext($1))', [this.config.clientId]);
      const value = await fn(db);
      await db.query('COMMIT');
      return value;
    } catch (error) {
      await db.query('ROLLBACK');
      throw error;
    } finally { db.release(); }
  }
  async close(): Promise<void> { await this.pool.end(); }
}
