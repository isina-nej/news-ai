# Ranking v1

`final = Σ w_i·s_i − penalties`, weights in DB, all sub-scores 0–100. Groups: NewsValue / AudienceFit / Momentum (see architecture.md). Raw engagement never direct; always via SourceBaselineService percentiles + velocity/acceleration. `algorithm_version="v1"`, full breakdown in ScoreRecord + DecisionLog.
