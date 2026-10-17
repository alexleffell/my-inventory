# ARD

## Shape

One Python process, server.py, standard library only. It serves HTML from public/ and JSON from data/. Photos live in captures/. Model files live in models/.

There is no application database in this repo. inventory.json is the item store. wishlist.json is the to-buy store. users.json holds salted password hashes and is gitignored.

Writes are atomic. A one-step bak file sits beside the JSON. A write that would empty a non-empty inventory keeps a timestamped copy first.

## Access

GET routes are public. POST routes require an admin once an admin exists. POST /api/wishlist is the public write. Basic auth is not encryption. Put the process behind HTTPS for a shared deploy.

Agents list items with GET /api/items, then load full rows with GET /api/items?ids=. They write identified items with POST /api/ingest. They do not edit inventory.json while the server is running.

## Deploy

deploy/ holds a container path and a systemd path. Both read the host from the environment. Data and captures are bind mounts on the container path. A rebuild that drops those mounts loses the inventory.

Do not rebuild the production container until a backup restore of data and captures has been rehearsed.

This repo is public. Do not commit a host, an address, a password, or a customer order id.

## Successor

The private Next.js rewrite uses a shared Postgres schema with an org id on customer rows. requireTenant() is the choke point. Vision identification is behind a provider switch. Billing is not built. Agents assigned to this project do not get that tree from this remote.
