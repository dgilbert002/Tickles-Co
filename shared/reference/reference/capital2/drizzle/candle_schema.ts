import { mysqlTable, varchar, decimal, timestamp, index } from "drizzle-orm/mysql-core";

/**
 * Candle data tables for each epic
 * Migrated from SQLite database_av.db to MySQL for consolidated database
 * 
 * Schema: timestamp (PK), open, high, low, close, volume
 * Index on timestamp for fast range queries
 */

export const soxlCandles = mysqlTable("SOXL_av_5min", {
  timestamp: varchar("timestamp", { length: 30 }).primaryKey(),
  open: decimal("open", { precision: 10, scale: 4 }).notNull(),
  high: decimal("high", { precision: 10, scale: 4 }).notNull(),
  low: decimal("low", { precision: 10, scale: 4 }).notNull(),
  close: decimal("close", { precision: 10, scale: 4 }).notNull(),
  volume: decimal("volume", { precision: 15, scale: 2 }).notNull(),
}, (table) => ({
  timestampIdx: index("timestamp_idx").on(table.timestamp),
}));

export const teclCandles = mysqlTable("TECL_av_5min", {
  timestamp: varchar("timestamp", { length: 30 }).primaryKey(),
  open: decimal("open", { precision: 10, scale: 4 }).notNull(),
  high: decimal("high", { precision: 10, scale: 4 }).notNull(),
  low: decimal("low", { precision: 10, scale: 4 }).notNull(),
  close: decimal("close", { precision: 10, scale: 4 }).notNull(),
  volume: decimal("volume", { precision: 15, scale: 2 }).notNull(),
}, (table) => ({
  timestampIdx: index("timestamp_idx").on(table.timestamp),
}));

export const spxsCandles = mysqlTable("SPXS_av_5min", {
  timestamp: varchar("timestamp", { length: 30 }).primaryKey(),
  open: decimal("open", { precision: 10, scale: 4 }).notNull(),
  high: decimal("high", { precision: 10, scale: 4 }).notNull(),
  low: decimal("low", { precision: 10, scale: 4 }).notNull(),
  close: decimal("close", { precision: 10, scale: 4 }).notNull(),
  volume: decimal("volume", { precision: 15, scale: 2 }).notNull(),
}, (table) => ({
  timestampIdx: index("timestamp_idx").on(table.timestamp),
}));

export const tecsCandles = mysqlTable("TECS_av_5min", {
  timestamp: varchar("timestamp", { length: 30 }).primaryKey(),
  open: decimal("open", { precision: 10, scale: 4 }).notNull(),
  high: decimal("high", { precision: 10, scale: 4 }).notNull(),
  low: decimal("low", { precision: 10, scale: 4 }).notNull(),
  close: decimal("close", { precision: 10, scale: 4 }).notNull(),
  volume: decimal("volume", { precision: 15, scale: 2 }).notNull(),
}, (table) => ({
  timestampIdx: index("timestamp_idx").on(table.timestamp),
}));

export type CandleData = {
  timestamp: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
};
