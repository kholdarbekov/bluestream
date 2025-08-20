-- Database index usage analysis for BlueStream platform
-- Use this script to monitor index performance and identify unused indexes
-- Run periodically to optimize database performance

-- 1. Show index usage statistics
SELECT 
    schemaname,
    tablename,
    indexname,
    idx_tup_read,
    idx_tup_fetch,
    idx_blks_read,
    idx_blks_hit,
    CASE 
        WHEN idx_tup_read + idx_tup_fetch = 0 THEN 'UNUSED'
        WHEN idx_tup_read + idx_tup_fetch < 100 THEN 'LOW USAGE'
        WHEN idx_tup_read + idx_tup_fetch < 1000 THEN 'MEDIUM USAGE'
        ELSE 'HIGH USAGE'
    END as usage_level
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
ORDER BY (idx_tup_read + idx_tup_fetch) DESC;

-- 2. Identify potentially unused indexes
SELECT 
    schemaname,
    tablename,
    indexname,
    pg_size_pretty(pg_relation_size(indexrelid)) as index_size,
    idx_tup_read,
    idx_tup_fetch
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
    AND idx_tup_read = 0 
    AND idx_tup_fetch = 0
    AND indexname NOT LIKE '%_pkey'  -- Exclude primary keys
ORDER BY pg_relation_size(indexrelid) DESC;

-- 3. Show index sizes
SELECT 
    tablename,
    indexname,
    pg_size_pretty(pg_relation_size(indexrelid)) as index_size,
    pg_size_pretty(pg_relation_size(relid)) as table_size,
    ROUND(100.0 * pg_relation_size(indexrelid) / pg_relation_size(relid), 2) as index_to_table_ratio
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
ORDER BY pg_relation_size(indexrelid) DESC;

-- 4. Most frequently used indexes
SELECT 
    schemaname,
    tablename,
    indexname,
    idx_tup_read + idx_tup_fetch as total_usage,
    idx_tup_read,
    idx_tup_fetch,
    pg_size_pretty(pg_relation_size(indexrelid)) as index_size
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
    AND (idx_tup_read + idx_tup_fetch) > 0
ORDER BY (idx_tup_read + idx_tup_fetch) DESC
LIMIT 20;

-- 5. Tables with the most indexes
SELECT 
    tablename,
    COUNT(*) as index_count,
    pg_size_pretty(SUM(pg_relation_size(indexrelid))) as total_index_size,
    pg_size_pretty(MAX(pg_relation_size(relid))) as table_size
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
GROUP BY tablename, relid
ORDER BY COUNT(*) DESC;

-- 6. Index hit ratio (should be close to 1.0 for good performance)
SELECT 
    indexname,
    CASE 
        WHEN idx_blks_hit + idx_blks_read = 0 THEN 0
        ELSE ROUND(idx_blks_hit::numeric / (idx_blks_hit + idx_blks_read), 4)
    END as hit_ratio,
    idx_blks_hit,
    idx_blks_read,
    idx_tup_read + idx_tup_fetch as usage_count
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
    AND (idx_blks_hit + idx_blks_read) > 0
ORDER BY hit_ratio ASC;

-- 7. Duplicate or similar indexes (manual review needed)
SELECT 
    t1.tablename,
    t1.indexname as index1,
    t2.indexname as index2,
    t1.idx_tup_read + t1.idx_tup_fetch as usage1,
    t2.idx_tup_read + t2.idx_tup_fetch as usage2
FROM pg_stat_user_indexes t1
JOIN pg_stat_user_indexes t2 ON t1.tablename = t2.tablename 
WHERE t1.indexname < t2.indexname
    AND t1.schemaname = 'public'
    AND t2.schemaname = 'public'
    -- This is a rough check - manual review needed for actual column overlap
ORDER BY t1.tablename, t1.indexname;

-- 8. Table and index sizes summary
SELECT 
    'Total Tables' as metric,
    COUNT(DISTINCT tablename)::text as value
FROM pg_stat_user_tables
WHERE schemaname = 'public'

UNION ALL

SELECT 
    'Total Indexes' as metric,
    COUNT(*)::text as value
FROM pg_stat_user_indexes
WHERE schemaname = 'public'

UNION ALL

SELECT 
    'Total Index Size' as metric,
    pg_size_pretty(SUM(pg_relation_size(indexrelid))) as value
FROM pg_stat_user_indexes
WHERE schemaname = 'public'

UNION ALL

SELECT 
    'Total Table Size' as metric,
    pg_size_pretty(SUM(DISTINCT pg_relation_size(relid))) as value
FROM pg_stat_user_indexes
WHERE schemaname = 'public';

-- 9. Query to reset index statistics (use carefully!)
-- Uncomment the following line if you want to reset statistics:
-- SELECT pg_stat_reset();

-- 10. Most expensive queries that could benefit from indexing
-- (requires pg_stat_statements extension)
SELECT 
    query,
    calls,
    total_time,
    mean_time,
    ROUND((100.0 * total_time / SUM(total_time) OVER()), 2) AS percentage
FROM pg_stat_statements
WHERE calls > 100  -- Only show frequently called queries
ORDER BY total_time DESC
LIMIT 10;

-- Instructions for running this analysis:
-- 1. Run this script after your application has been running for a while
-- 2. Look for unused indexes that can be dropped
-- 3. Check for indexes with low hit ratios
-- 4. Monitor which indexes are most frequently used
-- 5. Consider the index-to-table size ratio for optimization
--
-- Recommendations:
-- - Drop indexes with 0 usage after confirming they're not needed
-- - Investigate low hit ratio indexes
-- - Consider composite indexes for frequently combined WHERE clauses
-- - Monitor index growth over time