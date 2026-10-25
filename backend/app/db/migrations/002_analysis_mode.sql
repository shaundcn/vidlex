-- 2026-10-25：记录分析方式（技术选型 4.4、4.8）。
-- “超过 512MB，只用音频分析”以前写在操作提示 notice 里，状态一变就被清空；它是结果的属性，改成单独字段。
ALTER TABLE tasks ADD COLUMN analysis_mode TEXT CHECK (analysis_mode IN ('video', 'audio'));
UPDATE tasks SET analysis_mode = 'audio', notice = NULL WHERE notice = '超过 512MB，只用音频分析';
UPDATE tasks SET analysis_mode = 'video' WHERE analysis_mode IS NULL AND status = 'completed';
