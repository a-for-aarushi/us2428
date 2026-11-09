# internship-watch
Polls career APIs every 5 min → keyword gate → Claude (Haiku) eligibility check → ntfy push.

Setup:
  pip install -r requirements.txt
  export ANTHROPIC_API_KEY=...        # optional; without it, keyword matches are pushed unfiltered
  edit config.yaml: ntfy_topic         # subscribe to the same topic in the ntfy phone app
  python watcher.py --check            # see which sources work
  nohup python watcher.py > watch.log 2>&1 &   # first run seeds existing jobs silently

Add a company: append to `sources` in config.yaml (greenhouse/lever/ashby slug, or a Workday host/tenant/site
copied from the company's myworkdayjobs.com URL).
