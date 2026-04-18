import { mysqlTable, varchar, decimal, int, boolean, timestamp } from 'drizzle-orm/mysql-core';

/**
 * Market Information Table
 * Stores epic-specific trading rules and constraints
 */
export const marketInfo = mysqlTable('marketInfo', {
  epic: varchar('epic', { length: 50 }).primaryKey(),
  
  // Spread and pricing
  spreadPercent: decimal('spreadPercent', { precision: 10, scale: 6 }).notNull(), // e.g., 0.00019 for 0.019%
  
  // Contract limits
  minContractSize: decimal('minContractSize', { precision: 10, scale: 2 }).notNull(), // e.g., 1.00
  maxContractSize: decimal('maxContractSize', { precision: 15, scale: 2 }).notNull(), // e.g., 10000.00
  contractMultiplier: decimal('contractMultiplier', { precision: 10, scale: 2 }).notNull().default('1.00'), // Usually 1.00 for CFDs
  
  // Fees
  overnightFundingLongPercent: decimal('overnightFundingLongPercent', { precision: 10, scale: 6 }).notNull(), // e.g., -0.00023 for -0.023%
  overnightFundingShortPercent: decimal('overnightFundingShortPercent', { precision: 10, scale: 6 }).notNull(), // e.g., -0.00015
  
  // Trading hours (ET timezone)
  marketOpenTime: varchar('marketOpenTime', { length: 8 }).notNull(), // e.g., "09:30:00"
  marketCloseTime: varchar('marketCloseTime', { length: 8 }).notNull(), // e.g., "16:00:00"
  
  // Additional info
  currency: varchar('currency', { length: 10 }).notNull().default('USD'),
  isActive: boolean('isActive').notNull().default(true),
  
  // Metadata
  createdAt: timestamp('createdAt').notNull().defaultNow(),
  updatedAt: timestamp('updatedAt').notNull().defaultNow().onUpdateNow(),
});

export type MarketInfo = typeof marketInfo.$inferSelect;
export type NewMarketInfo = typeof marketInfo.$inferInsert;

