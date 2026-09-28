-- Публикация, которую модель не взяла в темы, больше не закрывается навсегда.
--
-- Раньше ranked_at проставлялся всем переданным источникам: из полусотни
-- модель делает десяток тем, а остальные сорок исчезали из выборки без
-- второго шанса. Теперь у них есть три захода — этого хватает, чтобы
-- публикация попала в другой набор кандидатов, и при этом очередь не
-- заполняется вечными неудачниками.
ALTER TABLE source_items
    ADD COLUMN ranking_attempts INTEGER NOT NULL DEFAULT 0
        CHECK (ranking_attempts >= 0);

DROP INDEX IF EXISTS source_items_unranked_idx;
CREATE INDEX source_items_unranked_idx
    ON source_items (published_at DESC NULLS LAST, fetched_at DESC)
    WHERE is_preprint = FALSE AND ranked_at IS NULL AND ranking_attempts < 3;
