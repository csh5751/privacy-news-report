CREATE TABLE IF NOT EXISTS articles (
  article_id TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL,
  topic TEXT NOT NULL, url TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS feedback_votes (
  article_id TEXT NOT NULL, voter_id TEXT NOT NULL, vote TEXT NOT NULL CHECK(vote IN ('up','down')),
  reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (article_id, voter_id), FOREIGN KEY(article_id) REFERENCES articles(article_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_updated ON feedback_votes(updated_at);
