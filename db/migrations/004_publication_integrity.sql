-- Публикация перестаёт зависеть от «текущей» версии материала.
--
-- До этой миграции очередь брала material_versions по current_version, и
-- правка после планирования подменяла текст: автор одобряла одно, а в
-- канал в назначенное время уходило другое, уже не одобренное.
ALTER TABLE publications ADD COLUMN material_version INTEGER;

UPDATE publications AS p
SET material_version = m.current_version
FROM materials AS m
WHERE m.id = p.material_id AND p.material_version IS NULL;

ALTER TABLE publications ALTER COLUMN material_version SET NOT NULL;
ALTER TABLE publications ADD CONSTRAINT publications_material_version_positive
    CHECK (material_version > 0);

-- Отметка о прохождении детерминированного линтера красных линий.
ALTER TABLE publications ADD COLUMN lint_checked_at TIMESTAMPTZ;

-- Кто одобрил и кто поставил в очередь: авторов теперь несколько.
ALTER TABLE materials ADD COLUMN approved_by BIGINT;
ALTER TABLE publications ADD COLUMN scheduled_by BIGINT;

-- Новый статус: отправка прервалась и неизвестно, ушёл ли пост.
-- Такую публикацию нельзя переотправлять автоматически — только человек,
-- заглянув в канал. Прежняя логика «перезабрать publishing старше пяти
-- минут» публиковала повторно.
ALTER TABLE publications DROP CONSTRAINT IF EXISTS publications_status_check;
ALTER TABLE publications ADD CONSTRAINT publications_status_check
    CHECK (status IN ('scheduled', 'publishing', 'published', 'failed', 'cancelled', 'held'));

DROP INDEX IF EXISTS publications_one_active_per_material_idx;
CREATE UNIQUE INDEX publications_one_active_per_material_idx
    ON publications (material_id)
    WHERE status IN ('scheduled', 'publishing', 'failed', 'held');
