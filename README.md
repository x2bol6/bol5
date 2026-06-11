# bo3ggapi

Unofficial BO3.gg REST API wrapper for Vercel/FastAPI.

Supports CS2, Valorant, Rainbow Six Siege, Dota 2, League of Legends, and MLBB.

## Deploy to Vercel

Upload this repo to GitHub, then import it in Vercel.

Files needed:

```text
main.py
app.py
requirements.txt
vercel.json
README.md
```

## Core endpoints

```text
GET /v2/games
GET /v2/health?game=cs2
GET /v2/match?game=cs2&q=live
GET /v2/match?game=cs2&q=finished
GET /v2/match/all?q=live
GET /v2/search?game=cs2&source=finished&q=Nemesis
GET /v2/debug/fetch?game=cs2&q=finished
```

## Verifier details endpoint

`/v2/match/details` now searches both live and finished by default when `q` is omitted. Internally BO3 uses `/matches/current` for live matches, but API metadata reports `live+finished`.

```text
GET /v2/match/details?game=cs2&team1=Team%20Nemesis&team2=FOKUS
GET /v2/match/details?game=cs2&search=Nemesis%20FOKUS
GET /v2/match/details?game=cs2&max_results=10
```

You can still force one source list:

```text
GET /v2/match/details?game=cs2&q=live&team1=Team%20Nemesis&team2=FOKUS
GET /v2/match/details?game=cs2&q=finished&team1=Team%20Nemesis&team2=FOKUS
```

Compact response includes series winner and individual map/game winners only. It does not include streams, lineups, picks/bans, or player lists.
