-- Реестр тем блога входит в конвейер наравне со свежим PubMed.
--
-- До этой миграции темы рождались только из потока публикаций, и повестку
-- задавало то, что на этой неделе вышло в журналах. Наверх всплывали
-- «Сурводютид при ожирении, фаза III» и «Антикоагулянты при ВТЭ» — темы для
-- коллег, а не для читателя, который ночью гуглит свой ТТГ. Реестр из
-- core/topics.yaml, написанный под аудиторию, в конвейере не участвовал.

-- Откуда пришла тема: из реестра блога или из свежего потока источников.
ALTER TABLE topics ADD COLUMN origin TEXT NOT NULL DEFAULT 'pubmed';
ALTER TABLE topics ADD CONSTRAINT topics_origin_check
    CHECK (origin IN ('registry', 'pubmed'));

-- Номер темы в core/topics.yaml: по нему статус возвращается в файл.
ALTER TABLE topics ADD COLUMN registry_id INTEGER;

-- Рубрика по разделу 8 системного промпта и ниша по разделу 4.
-- Без них всё сваливалось в Р7 «что нового» — одну рубрику из семи.
ALTER TABLE topics ADD COLUMN rubric TEXT;
ALTER TABLE topics ADD COLUMN niche TEXT;

-- Тема реестра существует в единственном экземпляре: повторный импорт
-- обновляет её, а не плодит дубли.
CREATE UNIQUE INDEX topics_registry_id_idx
    ON topics (registry_id)
    WHERE registry_id IS NOT NULL;

-- Реестровые темы не протухают: они ждут своей очереди месяцами, тогда как
-- свежая публикация теряет актуальность за неделю.
CREATE INDEX topics_candidate_origin_idx
    ON topics (origin, score DESC, last_seen_at DESC)
    WHERE status = 'candidate';

-- У материала фиксируются рубрика и ниша: по ним считаются доли раздела 4,
-- иначе «щитовидка 15%» остаётся цифрой в документе, а не в плане выпуска.
ALTER TABLE materials ADD COLUMN rubric TEXT;
ALTER TABLE materials ADD COLUMN niche TEXT;

CREATE INDEX materials_niche_created_idx
    ON materials (created_at DESC)
    WHERE niche IS NOT NULL;

-- Фактура автора под конкретную тему: расшифровка голосового, наговорённого
-- в ответ на «есть что добавить от себя». Приоритетнее источников — это то,
-- чего нет ни в одном абстракте.
ALTER TABLE topics ADD COLUMN author_note TEXT;
ALTER TABLE topics ADD COLUMN author_note_at TIMESTAMPTZ;

-- Поисковый запрос к PubMed под тему реестра: тема сформулирована по-русски
-- и словами читателя, а искать доказательную базу надо по-английски и
-- терминами. Запрос строится один раз и кэшируется — он не меняется.
ALTER TABLE topics ADD COLUMN evidence_query TEXT;
