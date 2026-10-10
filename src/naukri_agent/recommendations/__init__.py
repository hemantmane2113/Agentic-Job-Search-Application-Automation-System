"""
Daily match-digest recommendations.

The deterministic matcher (matching/scorer.py) is the ONLY source of the
suitability score and ranking. Application status comes ONLY from the
ApplicationHistory table. Freshness labels come ONLY from DB timestamps.
The LLM (optional) may add a short natural-language explanation over the
already-computed deterministic factors — it never sets the score,
status, freshness label, or URL.
"""
