"""SQLite transactions are serialized across API calls and the background worker."""

import asyncio
from contextlib import asynccontextmanager

import aiosqlite

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
CREATE TABLE IF NOT EXISTS vaults (
 contract_id TEXT PRIMARY KEY, sponsor_address TEXT NOT NULL, elder_address TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_ledger INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS spend_records (
 id TEXT PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES vaults(contract_id),
 onchain_tx_id TEXT, stellar_tx_hash TEXT UNIQUE,
 bank_code TEXT NOT NULL, account_number TEXT NOT NULL, account_name TEXT NOT NULL,
 beneficiary_hash TEXT NOT NULL, amount_usdc_stroops TEXT NOT NULL,
 amount_ngn TEXT NOT NULL, fx_rate TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING_ONCHAIN'
 CHECK(status IN ('PENDING_ONCHAIN','PAUSED','EXECUTED','DECLINED','PAYOUT_COMPLETED','PAYOUT_FAILED')),
 fincra_reference TEXT, payout_requested INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, notification_sent INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(contract_id, onchain_tx_id)
);
CREATE TABLE IF NOT EXISTS rpc_cursor (id INTEGER PRIMARY KEY CHECK(id=1), last_ledger INTEGER);
INSERT OR IGNORE INTO rpc_cursor(id, last_ledger) VALUES (1, NULL);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, contract_id TEXT NOT NULL, ledger INTEGER NOT NULL,
 tx_hash TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
 processed INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS events_pending ON events(processed, ledger, id);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()
        self.connection = None

    async def open(self):
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript(SCHEMA)
        await self.connection.commit()
        return self

    async def close(self):
        if self.connection:
            await self.connection.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            try:
                await self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def all(self, sql, args=()):
        async with self.lock, self.connection.execute(sql, args) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def one(self, sql, args=()):
        rows = await self.all(sql, args)
        return rows[0] if rows else None

    async def execute(self, sql, args=()):
        async with self.transaction() as conn:
            await conn.execute(sql, args)
