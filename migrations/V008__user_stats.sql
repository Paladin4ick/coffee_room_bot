-- Агрегированная статистика пользователя (кирчики через реакции + победы в играх)
CREATE TABLE user_stats (
    user_id   BIGINT NOT NULL,
    chat_id   BIGINT NOT NULL,
    -- Реакции
    score_given  INT NOT NULL DEFAULT 0,  -- сколько кирчиков подарено реакциями (сумма положит. дельт)
    score_taken  INT NOT NULL DEFAULT 0,  -- сколько кирчиков отнято реакциями (сумма модулей отриц. дельт)
    -- Победы в играх
    wins_blackjack INT NOT NULL DEFAULT 0,
    wins_slots     INT NOT NULL DEFAULT 0,
    wins_dice      INT NOT NULL DEFAULT 0,
    wins_giveaway  INT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, chat_id)
);

-- Дневной лимит положительных реакций от одного актора к одному таргету
CREATE TABLE daily_positive_limits (
    actor_id  BIGINT NOT NULL,
    target_id BIGINT NOT NULL,
    chat_id   BIGINT NOT NULL,
    date      DATE   NOT NULL,
    given     INT    NOT NULL DEFAULT 0,
    PRIMARY KEY (actor_id, target_id, chat_id, date)
);

CREATE INDEX idx_daily_positive_limits_date ON daily_positive_limits (date);
