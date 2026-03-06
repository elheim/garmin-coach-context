# Personal AI Coach vs GarminDB

| | **Personal AI Coach** | **[GarminDB](https://github.com/tcgoetz/GarminDB)** |
|---|---|---|
| **Purpose** | Generate AI-ready coaching context from Garmin data | Download, parse, and analyze Garmin data in SQLite |
| **Output** | Compact markdown file (~2-5k tokens) optimized for AI chat | Raw SQLite databases + Jupyter notebooks for manual analysis |
| **AI integration** | Built-in — Cursor rule, coaching methodology, fatigue flags, training load ratio | None — it's a data tool, you'd need to build the AI layer yourself |
| **Coaching logic** | Pre-computes weekly summaries, acute/chronic load, fatigue detection, health trends | Stores raw data — daily/weekly/monthly/yearly summaries in SQL, you query it yourself |
| **Data scope** | Activities + health metrics (HRV, sleep, stress, body battery, training readiness, VO2 max) | Much broader — FIT file parsing, GPS tracks, laps, records, TCX export, Connect IQ plugins, FitBit/MS Health import |
| **Maturity** | New, ~14 files | 2.9k stars, 966 commits, 50 releases, 20 contributors |
| **Complexity** | ~700 lines of Python, 5 min setup | Large project with submodules, Makefiles, config files, Jupyter notebooks |
| **Visualization** | None (the AI is your interface) | Jupyter notebooks, command-line graphs |
| **Credential storage** | OS keychain (macOS Keychain / Linux SecretService / Windows Credential Locker) | JSON config file (`~/.GarminDb/GarminConnectConfig.json`) |
| **Works with** | Any AI — Cursor, ChatGPT, Claude, etc. | SQL tools, Jupyter, SQLite browsers |
